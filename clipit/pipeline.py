"""One VOD in, finished clips out.

Every stage caches its result in out/<vod-id>/ (written atomically), so a rerun skips finished work.
Cross-run state (which VODs are done, which clips exist) is in the SQLite catalog, see clipit.db.
"""

import functools
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from clipit import ai, facecam, moments, render, source, transcribe, twitch
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


def clip_file(rank: int, clip: dict) -> str:
    """Path under the work folder. The time range is in it so a re-picked moment never reuses an old render."""
    return f"clips/{rank:02d}_{slug(clip['title'])}_{clip['start']:.1f}-{clip['end']:.1f}.mp4"


def get_chat(info: dict) -> list:
    if not info.get("extractor", "").startswith("twitch:vod"):
        return []  # other sites: no chat replay, loudness alone picks the moments
    log("fetching chat replay")
    try:
        return twitch.fetch_chat(info["id"].lstrip("v"), info["duration"])
    except Exception as e:
        # Chat is the strongest signal for Twitch: fail the VOD so it's retried later, rather than make worse clips.
        # (A VOD with chat disabled isn't an error: it just returns no messages.)
        raise ClipitError(f"couldn't fetch chat replay ({type(e).__name__}: {e}), will retry later") from e


def process_vod(
    url: str, n_clips: int = 6, ai_cmd: str | None = None, whisper: str = "small", out_root: Path = ROOT / "out"
) -> tuple[Path, list[dict]]:
    """Make up to n_clips clips from a VOD. Returns (work folder, rendered clips). Raises ClipitError when it can't."""
    info = source.fetch_info(url)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(info["id"])):  # it becomes a folder name
        raise ClipitError(f"unexpected video id {info['id']!r}")
    dur = float(info["duration"])
    if dur > MAX_DURATION:
        raise ClipitError(f"VOD is {mmss(dur)} long, over the {mmss(MAX_DURATION)} limit")
    work = Path(out_root) / info["id"]
    work.mkdir(parents=True, exist_ok=True)
    log(f"{info.get('title')} ({mmss(dur)}) -> {work}")

    # Audio is only needed to measure loudness and transcribe: a rerun with those cached skips the big download.
    audio = functools.cache(lambda: source.download_audio(url, work))
    msgs = cached_json(work / "chat.json", lambda: get_chat(info)) or []
    log(f"{len(msgs)} chat messages")

    loud_file = work / "loudness.npy"
    if not loud_file.exists():
        log("measuring loudness")
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
        for i, (w, segs) in enumerate(zip(todo, transcribe.transcribe(audio(), todo, whisper), strict=True), 1):
            tr[f"{w[0]}-{w[1]}"] = segs
            write_atomic(tr_file, json.dumps(tr))
            log(f"  {i}/{len(todo)} @ {mmss(w[0])}")

    def ask():
        cands = []
        for s, e in windows:
            stats = f"audio peak +{loud[s:e].max() - np.median(loud):.0f} dB"
            if rate.any():
                stats = f"chat {rate[s:e].mean() / rate.mean():.1f}x normal, " + stats
            cands.append(
                {"start": s, "end": e, "segments": tr[f"{s}-{e}"], "stats": stats, "chat": [m for m in msgs if s <= m[0] < e]}
            )
        log(f"asking the AI to pick {n_clips} clips from {len(cands)} candidates")
        return ai.pick_clips(ai_cmd, info, cands, n_clips, work)

    picks = cached_json(work / "picks.json", ask)  # delete picks.json to re-ask the AI

    urls = source.stream_urls(url)

    def detect():
        log("looking for webcam layouts")
        return facecam.find_layouts(urls[0], dur)

    layouts = cached_json(work / "facecam_layouts.json", detect)  # delete it to re-detect
    log(f"{len(layouts)} webcam layout(s) found" if layouts else "no webcam found: blurred-fill layout")
    (work / "clips").mkdir(exist_ok=True)
    words = [w for segs in tr.values() for s in segs for w in s["words"]]
    rendered = []
    for i, c in enumerate(picks, 1):
        c["idx"], c["file"] = i, clip_file(i, c)
        out = work / c["file"]
        if not out.exists():
            cam = facecam.clip_cam(urls[0], layouts, c["start"], c["end"])  # streamers switch scene layouts mid-stream
            log(f"rendering {out.name} ({c['end'] - c['start']:.0f}s, {'facecam on top' if cam else 'blurred fill'})")
            try:
                render.render(urls, c, [w for w in words if c["start"] - 0.05 <= w[0] and w[1] <= c["end"] + 0.3], out, cam)
            except subprocess.CalledProcessError as e:
                logger.error("render failed for %s: %s", out.name, e.stderr[-800:])
                continue
            except subprocess.TimeoutExpired:
                logger.error("render timed out for %s", out.name)
                continue
        rendered.append(c)
    if not rendered:
        raise ClipitError("every clip failed to render, see the log")

    write_atomic(work / "picks.json", json.dumps(picks, indent=2))
    render.write_posts(work / "posts.md", info, url, rendered)
    log(f"done: {len(rendered)} clips in {work / 'clips'} (captions + titles in {work / 'posts.md'})")
    return work, rendered
