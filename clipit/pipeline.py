"""One VOD in, finished clips out.

Every stage caches its result in out/<vod-id>/ (written atomically), so a rerun skips finished work.
Cross-run state (which VODs are done, which clips exist) is in the SQLite catalog, see clipit.db.
"""

import functools
import json
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from clipit import ai, db, edit, facecam, kick, moments, render, runs, source, transcribe, twitch
from clipit.util import ROOT, ClipitError, log, logger, mmss, slug, write_atomic

MAX_DURATION = 48 * 3600  # bounds disk use (~2.5 GB of audio); longer VODs are skipped
MIN_SCORE = 5  # picks the AI scored under this (1-10) are dropped: trash nobody would keep, not worth rendering


def cached_json(path: Path, make: Callable[[], Any]) -> Any:
    """Load path, or build it with make() and save it. make() returning None means don't cache (retry next run)."""
    if not path.exists():
        value = make()
        if value is None:
            return None
        write_atomic(path, json.dumps(value))
    return json.loads(path.read_text(encoding="utf-8"))


def clip_file(rank: int, clip: dict, again: str = "") -> str:
    """Path under the work folder. The time range is in it so a re-picked moment never reuses an old render, and a
    re-edit's time (again) so it never writes over the video it replaces, which may be playing in the dashboard."""
    edited = "_edited" if clip.get("edit") else ""  # a high-effort version of a moment never reuses the plain render
    return f"clips/{rank:02d}_{slug(clip['title'])}_{clip['start']:.1f}-{clip['end']:.1f}{edited}{again}.mp4"


def streamer(info: dict) -> tuple[str, str]:
    """(login, site) to credit in the post text, ('', '') for other sites. Checked: it lands in text and hashtags."""
    login, extractor = str(info.get("uploader_id") or ""), info.get("extractor", "")
    if extractor.startswith("twitch:vod") and re.fullmatch(r"[A-Za-z0-9_]{1,25}", login):
        return login, "twitch.tv"
    if extractor == "kick:vod" and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", login):
        return login, "kick.com"
    return "", ""


def get_chat(info: dict) -> list:
    extractor = info.get("extractor", "")
    if not extractor.startswith("twitch:vod") and extractor != "kick:vod":
        return []  # other sites: no chat replay, loudness alone picks the moments
    log("fetching chat replay")
    try:
        if extractor == "kick:vod":
            return kick.fetch_chat(int(info["channel_id"]), float(info["timestamp"]), info["duration"])
        return twitch.fetch_chat(info["id"].lstrip("v"), info["duration"])
    except Exception as e:
        # Chat is the strongest signal: fail the VOD so it's retried later, rather than make worse clips.
        # (A VOD with chat disabled isn't an error: it just returns no messages.)
        raise ClipitError(f"couldn't fetch chat replay ({type(e).__name__}: {e}), will retry later") from e


def progress(stage: str, done: int | None = None, of: int | None = None) -> None:
    """The stage this VOD is at, for a dashboard run's Run tab, with how far along it is when it's counted."""
    runs.progress(stage=stage, count=[done, of] if of else None)


def process_vod(
    url: str,
    n_clips: int = 6,
    ai_cmd: str | None = None,
    whisper: str = "turbo",
    out_root: Path = ROOT / "out",
    info: dict | None = None,
    high_effort: bool = False,
    clipped: Sequence[tuple[int, int]] = (),
) -> tuple[Path, list[dict]]:
    """Make up to n_clips clips from a VOD, none the AI scored under MIN_SCORE. Returns (work folder, rendered clips). Raises ClipitError when it can't.
    `info` is source.fetch_info(url), when the caller already has it. high_effort: the AI edits each pick scored db.AUTO_SCORE+
    and reviews it until it's done (clipit.edit), into picks_edited.json and posts_edited.md so the plain clips stay as they are.
    clipped: (second, views) of moments people clipped in this VOD (site.clip_moments), looked at first."""
    info = info or source.fetch_info(url)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(info["id"])):  # it becomes a folder name
        raise ClipitError(f"unexpected video id {info['id']!r}")
    dur = float(info["duration"])
    if dur > MAX_DURATION:
        raise ClipitError(f"VOD is {mmss(dur)} long, over the {mmss(MAX_DURATION)} limit")
    work = Path(out_root) / info["id"]
    work.mkdir(parents=True, exist_ok=True)
    log(f"{info.get('title')} ({mmss(dur)}) -> {work}")

    # Audio is only needed to measure loudness and transcribe: a rerun with those cached skips the big download.
    loud_file = work / "loudness.npy"
    stop = threading.Event()
    progress("Downloading the audio and chat")
    with ThreadPoolExecutor(1) as pool:  # chat and audio both wait on the network: fetch them at the same time
        dl = None if loud_file.exists() else pool.submit(source.download_audio, url, work, stop)
        try:
            msgs = cached_json(work / "chat.json", lambda: get_chat(info)) or []
        except BaseException:
            stop.set()  # don't sit through a multi-GB download just to report the chat error
            raise
    audio = functools.cache(lambda: dl.result() if dl else source.download_audio(url, work))
    log(f"{len(msgs)} chat messages")

    if not loud_file.exists():
        log("measuring loudness")
        progress("Measuring loudness")
        tmp = work / "loudness.tmp.npy"
        np.save(tmp, moments.loudness(audio()))
        tmp.replace(loud_file)
    score, loud, rate = moments.signals(dur, np.load(loud_file), msgs)
    windows = moments.windows(score, max(10, n_clips * 3), dur, clipped)

    tr_file = work / "transcripts.json"
    tr = json.loads(tr_file.read_text(encoding="utf-8")) if tr_file.exists() else {}
    todo = [w for w in windows if f"{w[0]}-{w[1]}" not in tr]
    if todo:
        log(f"transcribing {len(todo)} candidate moments (whisper {whisper})")
        progress("Transcribing the best moments", 0, len(todo))
        for i, (w, segs) in enumerate(zip(todo, transcribe.transcribe(audio(), todo, whisper), strict=True), 1):
            tr[f"{w[0]}-{w[1]}"] = segs
            write_atomic(tr_file, json.dumps(tr))
            log(f"  {i}/{len(todo)} @ {mmss(w[0])}")
            progress("Transcribing the best moments", i, len(todo))

    cands = candidates(windows, tr, msgs, loud, rate, clipped)

    def ask():
        log(f"asking the AI to pick up to {n_clips} clips from {len(cands)} candidates")
        progress(f"The AI is picking up to {n_clips} clips from {len(cands)} moments")
        return ai.pick_clips(ai_cmd, info, cands, n_clips, work)

    picks = cached_json(work / "picks.json", ask)  # delete picks.json to re-ask the AI
    if dropped := [p for p in picks if p["score"] < MIN_SCORE]:
        log(f"dropping {len(dropped)} of {len(picks)} picks the AI scored under {MIN_SCORE}")
        picks = [p for p in picks if p["score"] >= MIN_SCORE]
    if not picks:
        log("no pick worth rendering: the VOD has no good clips")
        return work, []
    words = [w for segs in tr.values() for s in segs for w in s["words"]]
    picks_file, posts_file = ("picks_edited.json", "posts_edited.md") if high_effort else ("picks.json", "posts.md")
    urls = source.stream_urls(url)
    layouts = webcam_layouts(work, urls, dur)
    if high_effort:
        (work / "edit").mkdir(exist_ok=True)
        edited = []
        for i, p in enumerate(picks, 1):  # each one cached on its own: a crash keeps the clips already done
            if p["score"] < db.AUTO_SCORE:  # too weak to ever post on its own: not worth the AI's editing rounds
                log(f"clip {i}/{len(picks)} scored {p['score']:g}, under {db.AUTO_SCORE}: kept plain, not edited")
                edited.append(p)
                continue
            log(f"editing clip {i}/{len(picks)} until it's done (up to {edit.MAX_ROUNDS} AI reviews)")
            progress("The AI is editing and reviewing the clips", i - 1, len(picks))
            polish = functools.partial(
                edit.polish, ai_cmd, info, p, cands, words, loud, work / "edit", look=preview(url, layouts, words)
            )
            edited.append(cached_json(work / "edit" / f"{p['start']:.1f}-{p['end']:.1f}.json", polish))
        picks = edited
        urls = source.stream_urls(url)  # editing can outlast the links fetched before it

    (work / "clips").mkdir(exist_ok=True)
    rendered, (login, site) = [], streamer(info)
    for i, c in enumerate(picks, 1):
        # picks.json cached before descriptions existed; capped like a new one, so credit + hashtags still fit TikTok's limit
        c.setdefault("description", c.get("caption", "")[: ai.TEXT_FIELDS["description"]])
        c["idx"], c["file"], c["streamer"], c["site"] = i, clip_file(i, c), login, site
        c["caption"], c["youtube"] = render.post_texts(c)
        out = work / c["file"]
        if not out.exists():
            progress("Rendering the clips", i - 1, len(picks))
            try:
                render_clip(urls, layouts, c, words, out)
            except subprocess.CalledProcessError as e:
                logger.error("render failed for %s: %s", out.name, e.stderr[-800:])
                continue
            except ClipitError as e:  # the stream couldn't be read: the other clips may still work
                logger.error("render failed for %s: %s", out.name, e)
                continue
            except subprocess.TimeoutExpired:
                logger.error("render timed out for %s", out.name)
                continue
        rendered.append(c)
    if not rendered:
        raise ClipitError("every clip failed to render, see the log")

    for i, c in enumerate(rendered, 1):
        progress("A reviewer is watching the clips", i - 1, len(rendered))
        review(ai_cmd, info, c, words, work, functools.partial(remade, url, layouts, words, cands, dur, work))

    # dropped picks stay in the file: an old clip of one, kept by the user, is found there to re-edit it (made_as)
    write_atomic(work / picks_file, json.dumps([*picks, *dropped], indent=2))
    render.write_posts(work / posts_file, info, url, rendered)
    log(f"done: {len(rendered)} clips in {work / 'clips'} (captions + titles in {work / posts_file})")
    return work, rendered


def watch(cmd: str | None, info: dict, clip: dict, words: list, work: Path) -> list[str]:
    """What a separate reviewer found wrong with the finished clip, watching all of it (ai.veto)."""
    said = sorted({(edit.clip_time(clip, w[0]), w[2]) for w in words if clip["start"] <= w[0] < clip["end"]})  # windows overlap
    spoken = " ".join(f"[{t:.0f}s] {w}" if i % 8 == 0 else w for i, (t, w) in enumerate(said))
    length = sum(b - a for a, b in edit.kept(clip))  # the edit's cuts make the video shorter than its time range
    with tempfile.TemporaryDirectory(prefix="clipit-") as tmp:
        try:
            sheets = render.sheets(work / clip["file"], Path(tmp))
        except (ClipitError, subprocess.SubprocessError):
            sheets = []  # veto stops a clip it can't see
        return ai.veto(cmd, clip, clip.get("streamer") or info.get("uploader"), spoken, sheets, length)


def review(
    cmd: str | None,
    info: dict,
    clip: dict,
    words: list,
    work: Path,
    remake: Callable[[dict, float, float], bool] | None = None,
) -> None:
    """A separate reviewer watches a clip good enough to post by itself. If it fails any check, the clip is cut again once
    around the peak the reviewer saw (remake: the picker only reads words) and watched again. If it still fails, its
    score drops under db.AUTO_SCORE and the reasons go in "why", so it stays for the user to judge but never posts alone.
    A review that never happened (the AI failed) stops the clip too, and is asked again if the VOD is run again."""
    clip.setdefault("picked_score", clip["score"])
    if clip["picked_score"] < db.AUTO_SCORE or clip.get("reviewed"):
        return
    failed = watch(cmd, info, clip, words, work)
    seen = clip.pop("seen", None)
    if failed and seen and remake and not clip.get("edit") and remake(clip, *seen):  # an edit's times are tied to its cut
        failed = watch(cmd, info, clip, words, work)
        clip.pop("seen", None)
    clip["reviewed"] = not any(f.startswith("review:") for f in failed)
    clip["score"] = db.AUTO_SCORE - 1.0 if failed else clip["picked_score"]
    if failed:
        log(f"review stopped {clip['file']}: " + "; ".join(failed))
        clip["why"] = ("Review failed: " + "; ".join(failed))[: ai.TEXT_FIELDS["why"]]


def remade(
    url: str, layouts: list, words: list, cands: list[dict], dur: float, work: Path, clip: dict, peak: float, end: float
) -> bool:
    """Render the clip again around the peak its reviewer saw, in place of the old video. False leaves it as it was."""
    cut = ai.recut(clip, peak, end, cands, dur)
    if not cut:
        return False
    new = {**clip, "start": cut[0], "end": cut[1]}
    new["file"] = clip_file(clip["idx"], new)
    log(f"cutting {clip['file']} again: {cut[0]:g}-{cut[1]:g} s, where the reviewer saw the peak")
    try:
        render_clip(source.stream_urls(url), layouts, new, words, work / new["file"])
    except (subprocess.SubprocessError, ClipitError) as e:
        logger.warning("couldn't cut %s again: %s", clip["file"], e)
        return False
    (work / clip["file"]).unlink(missing_ok=True)
    clip.update(new)
    return True


def candidates(
    windows: list[tuple[int, int]],
    tr: dict,
    msgs: list,
    loud: np.ndarray,
    rate: np.ndarray,
    clipped: Sequence[tuple[int, int]] = (),
) -> list[dict]:
    """The moments the AI picks from: transcript, chat and how loud and busy each one is."""
    cands = []
    for s, e in windows:
        stats = f"audio peak +{loud[s:e].max() - np.median(loud):.0f} dB"
        if rate.any():
            stats = f"chat {rate[s:e].mean() / rate.mean():.1f}x normal, " + stats
        if views := sum(v for t, v in clipped if s <= t < e):
            stats = f"viewers clipped this, {views} views, " + stats
        cands.append(
            {"start": s, "end": e, "segments": tr[f"{s}-{e}"], "stats": stats, "chat": [m for m in msgs if s <= m[0] < e]}
        )
    return cands


def webcam_layouts(work: Path, urls: list[str], dur: float) -> list:
    def detect():
        log("looking for webcam layouts")
        progress("Looking for the webcam")
        return facecam.find_layouts(urls[0], dur)

    layouts = cached_json(work / "facecam_layouts.json", detect)  # delete it to re-detect
    log(f"{len(layouts)} webcam layout(s) found" if layouts else "no webcam found: blurred-fill layout")
    return layouts


def render_clip(urls: list[str], layouts: list, c: dict, words: list, out: Path) -> None:
    """Render one clip, captioned with the words said in it."""
    cam = facecam.clip_cam(urls[0], layouts, c["start"], c["end"])  # streamers switch scene layouts mid-stream
    edit = c.get("edit") or {}
    if not cam and "layout" not in edit and (face := facecam.clip_face(urls[0], c["start"], c["end"])) is not None:
        c = {**c, "edit": {**edit, "layout": "crop", "focus": face}}  # a person filling the shot fills the phone too
    how = "facecam on top" if cam else "cropped on the face" if (c.get("edit") or {}).get("layout") == "crop" else "blurred fill"
    log(f"rendering {out.name} ({c['end'] - c['start']:.0f}s, {how})")
    render.render(urls, c, [w for w in words if c["start"] - 0.05 <= w[0] and w[1] <= c["end"] + 0.3], out, cam)


def preview(url: str, layouts: list, words: list) -> Callable[[dict, Path], list[tuple[str, Path]]]:
    """edit.polish's look: the stream itself at the version's middle, then the version rendered into folder, as the
    pictures its review sees. Each look fetches fresh stream links: editing every clip can outlast them. A failed
    render logs a warning and the review goes on without them (the clip's real render reports the error too)."""

    def look(clip: dict, folder: Path) -> list[tuple[str, Path]]:
        folder.mkdir(exist_ok=True)
        video, stream = folder / "preview.mp4", folder / "stream.jpg"
        try:
            urls = source.stream_urls(url)
            render.source_frame(urls[0], (clip["start"] + clip["end"]) / 2, stream)
            render_clip(urls, layouts, clip, words, video)
            frames = render.frames(video, edit.shown(clip), folder)
            # ffmpeg can succeed without writing a picture (frames() checks its own too)
            first = [("the stream", stream)] if stream.exists() else []
            return [*first, *((f"{t:.1f} s", f) for t, f in frames)]
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ClipitError) as e:
            logger.warning("couldn't render frames for the review: %s", str(getattr(e, "stderr", "") or e)[-500:])
            return []
        finally:
            video.unlink(missing_ok=True)

    return look


def reedit(url: str, clip: dict, request: str, at: float | None, ai_cmd: str | None, out_root: Path) -> dict:
    """The clip (db.reedit_source) edited again by the AI until it does what the user asked, and rendered: the edited
    clip of its moment, its file relative to out_root. `at` is the second of the clip's video they marked. Needs the
    VOD's working files: raises ClipitError when they're gone."""
    work = Path(out_root) / clip["vod_id"]  # the folder name was checked when the VOD was clipped
    files = {n: work / n for n in ("loudness.npy", "chat.json", "transcripts.json")}
    if not all(f.exists() for f in files.values()):
        raise ClipitError("this VOD's working files were deleted, so its clips can't be re-edited")
    info = source.fetch_info(url)
    if str(info["id"]) != clip["vod_id"]:
        raise ClipitError(f"the VOD link now opens another video ({info['id']!r})")
    msgs = json.loads(files["chat.json"].read_text(encoding="utf-8"))
    tr = json.loads(files["transcripts.json"].read_text(encoding="utf-8"))
    _, loud, rate = moments.signals(float(info["duration"]), np.load(files["loudness.npy"]), msgs)
    windows = [(int(s), int(e)) for s, e in (k.split("-") for k in tr)]
    pick = clip["pick"] or made_as(work, clip)
    words = [w for segs in tr.values() for s in segs for w in s["words"]]
    (work / "edit").mkdir(exist_ok=True)
    log(f"re-editing clip {clip['id']}: {request}")
    progress("The AI is re-editing the clip and reviewing it")
    at_vod = None if at is None else edit.source_time(pick, at)
    layouts = webcam_layouts(work, source.stream_urls(url), float(info["duration"]))
    cands = candidates(windows, tr, msgs, loud, rate)
    look = preview(url, layouts, words)
    new = edit.polish(ai_cmd, info, pick, cands, words, loud, work / "edit", request, at_vod, look)
    urls = source.stream_urls(url)  # the edit can outlast the links fetched before it
    login, site = streamer(info)
    c = {**pick, **new, "idx": clip["idx"], "streamer": login, "site": site}
    c["file"] = clip_file(clip["idx"], c, time.strftime("_%Y%m%d%H%M%S"))
    c["caption"], c["youtube"] = render.post_texts(c)
    (work / "clips").mkdir(exist_ok=True)
    progress("Rendering the clip")
    try:
        render_clip(urls, layouts, c, words, work / c["file"])
    except subprocess.CalledProcessError as e:  # a ClipitError from reading the stream already says what failed
        raise ClipitError(f"rendering the re-edit failed: {str(e.stderr)[-500:]}") from e
    except subprocess.TimeoutExpired as e:
        raise ClipitError("rendering the re-edit timed out") from e
    return {**c, "file": f"{work.name}/{c['file']}"}


def made_as(work: Path, clip: dict) -> dict:
    """How the AI made a clip from before the catalog kept it (db clips.pick): from its VOD's picks file."""
    path = work / ("picks_edited.json" if clip["variant"] == "edited" else "picks.json")
    picks = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    for p in picks:
        if (p["start"], p["end"]) == (clip["start_s"], clip["end_s"]):
            return p
    raise ClipitError("couldn't find how the AI made this clip in its VOD's files")
