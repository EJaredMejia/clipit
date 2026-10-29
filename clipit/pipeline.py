"""One VOD in, finished clips out.

Every stage caches its result in out/<vod-id>/ (written atomically), so a rerun skips finished work.
Cross-run state (which VODs are done, which clips exist) is in the SQLite catalog, see clipit.db.
"""

import functools
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from clipit import ai, db, edit, facecam, kick, moments, render, runs, source, transcribe, twitch
from clipit.util import ROOT, ClipitError, log, logger, mmss, slug, write_atomic

MAX_DURATION = 48 * 3600  # bounds disk use (~2.5 GB of audio); longer VODs are skipped


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
) -> tuple[Path, list[dict]]:
    """Make up to n_clips clips from a VOD. Returns (work folder, rendered clips). Raises ClipitError when it can't.
    `info` is source.fetch_info(url), when the caller already has it. high_effort: the AI edits each pick scored db.AUTO_SCORE+
    and reviews it until it's done (clipit.edit), into picks_edited.json and posts_edited.md so the plain clips stay as they are."""
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
    windows = moments.windows(score, max(10, n_clips * 3), dur)

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

    cands = candidates(windows, tr, msgs, loud, rate)

    def ask():
        log(f"asking the AI to pick {n_clips} clips from {len(cands)} candidates")
        progress(f"The AI is picking {n_clips} clips from {len(cands)} moments")
        return ai.pick_clips(ai_cmd, info, cands, n_clips, work)

    picks = cached_json(work / "picks.json", ask)  # delete picks.json to re-ask the AI
    words = [w for segs in tr.values() for s in segs for w in s["words"]]
    picks_file, posts_file = ("picks_edited.json", "posts_edited.md") if high_effort else ("picks.json", "posts.md")
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
            polish = functools.partial(edit.polish, ai_cmd, info, p, cands, words, loud, work / "edit")
            edited.append(cached_json(work / "edit" / f"{p['start']:.1f}-{p['end']:.1f}.json", polish))
        picks = edited

    urls = source.stream_urls(url)
    layouts = webcam_layouts(work, urls, dur)
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
            except subprocess.TimeoutExpired:
                logger.error("render timed out for %s", out.name)
                continue
        rendered.append(c)
    if not rendered:
        raise ClipitError("every clip failed to render, see the log")

    write_atomic(work / picks_file, json.dumps(picks, indent=2))
    render.write_posts(work / posts_file, info, url, rendered)
    log(f"done: {len(rendered)} clips in {work / 'clips'} (captions + titles in {work / posts_file})")
    return work, rendered


def candidates(windows: list[tuple[int, int]], tr: dict, msgs: list, loud: np.ndarray, rate: np.ndarray) -> list[dict]:
    """The moments the AI picks from: transcript, chat and how loud and busy each one is."""
    cands = []
    for s, e in windows:
        stats = f"audio peak +{loud[s:e].max() - np.median(loud):.0f} dB"
        if rate.any():
            stats = f"chat {rate[s:e].mean() / rate.mean():.1f}x normal, " + stats
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
    log(f"rendering {out.name} ({c['end'] - c['start']:.0f}s, {'facecam on top' if cam else 'blurred fill'})")
    render.render(urls, c, [w for w in words if c["start"] - 0.05 <= w[0] and w[1] <= c["end"] + 0.3], out, cam)


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
    new = edit.polish(ai_cmd, info, pick, candidates(windows, tr, msgs, loud, rate), words, loud, work / "edit", request, at_vod)
    login, site = streamer(info)
    c = {**pick, **new, "idx": clip["idx"], "streamer": login, "site": site}
    c["file"] = clip_file(clip["idx"], c, time.strftime("_%Y%m%d%H%M%S"))
    c["caption"], c["youtube"] = render.post_texts(c)
    urls = source.stream_urls(url)
    layouts = webcam_layouts(work, urls, float(info["duration"]))
    (work / "clips").mkdir(exist_ok=True)
    progress("Rendering the clip")
    try:
        render_clip(urls, layouts, c, words, work / c["file"])
    except subprocess.CalledProcessError as e:
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
