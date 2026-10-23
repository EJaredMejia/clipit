"""Talking to the video site through yt-dlp: metadata, audio-only download, stream URLs to seek into."""

import json
import subprocess
import threading
import time
from pathlib import Path

from clipit.util import ClipitError, log, sh, ytdlp

DOWNLOAD_TIMEOUT = 3 * 3600  # a 48 h VOD's audio is ~2.5 GB


def fetch_info(url: str) -> dict:
    info = json.loads(sh(ytdlp("-J", url), timeout=120).stdout)
    if not info.get("duration"):
        raise ClipitError("No duration: live streams aren't supported, pass the VOD link once the stream ends")
    return info


def download_audio(url: str, work: Path, stop: threading.Event | None = None) -> Path:
    """Audio-only download, cached. Setting stop kills it (the pipeline does when chat fails meanwhile)."""
    done = work / "audio.txt"  # yt-dlp writes the path only after the download finished
    if not done.exists():
        log("downloading audio only")
        p = subprocess.Popen(
            ytdlp(
                "-N",
                "32",  # Twitch serves ~10 s fragments; the download waits on request latency, not bandwidth
                "-f",
                "ba/worst",
                "--fixup",
                "never",  # remuxing Twitch's MPEG-TS into MP4 took 2+ min on a 24 h VOD; ffmpeg reads the raw file as fast
                "--no-simulate",
                "--print-to-file",
                "after_move:filepath",
                str(done),
                "-o",
                str(work / "audio.%(ext)s"),
                url,
            )
        )
        deadline, stop = time.monotonic() + DOWNLOAD_TIMEOUT, stop or threading.Event()
        while p.poll() is None:
            if stop.wait(1) or time.monotonic() > deadline:
                p.kill()
                p.wait()
                raise ClipitError("audio download " + ("stopped" if stop.is_set() else f"took over {DOWNLOAD_TIMEOUT}s"))
        if p.returncode:
            raise ClipitError(f"yt-dlp could not download the audio (exit {p.returncode})")
    return Path(done.read_text(encoding="utf-8").strip().splitlines()[-1])


def stream_urls(url: str) -> list[str]:
    """Direct 1080p URL(s), video first. Signed by Twitch for hours, so fetch fresh per run."""
    return sh(ytdlp("-g", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", url), timeout=120).stdout.split()
