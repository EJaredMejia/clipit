"""Talking to the video site through yt-dlp: metadata, audio-only download, stream URLs to seek into."""

import json
import subprocess
from pathlib import Path

from clipit.util import ClipitError, log, sh, ytdlp

DOWNLOAD_TIMEOUT = 3 * 3600  # a 48 h VOD's audio is ~2.5 GB


def fetch_info(url: str) -> dict:
    info = json.loads(sh(ytdlp("-J", url), timeout=120).stdout)
    if not info.get("duration"):
        raise ClipitError("No duration: live streams aren't supported, pass the VOD link once the stream ends")
    return info


def download_audio(url: str, work: Path) -> Path:
    done = work / "audio.txt"  # yt-dlp writes the path only after the download finished
    if not done.exists():
        log("downloading audio only")
        subprocess.run(
            ytdlp("-N", "8", "-f", "ba/worst", "--no-simulate", "--print-to-file", "after_move:filepath",
                  str(done), "-o", str(work / "audio.%(ext)s"), url),
            check=True, timeout=DOWNLOAD_TIMEOUT,
        )
    return Path(done.read_text(encoding="utf-8").strip().splitlines()[-1])


def stream_urls(url: str) -> list[str]:
    """Direct 1080p URL(s), video first. Signed by Twitch for hours, so fetch fresh per run."""
    return sh(ytdlp("-g", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", url), timeout=120).stdout.split()
