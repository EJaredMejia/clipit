"""Talking to the video site through yt-dlp: metadata, audio-only download, stream URLs to seek into."""

import functools
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from clipit.util import REMOTE, ClipitError, log, sh, ytdlp

DOWNLOAD_TIMEOUT = 3 * 3600  # a 48 h VOD's audio is ~2.5 GB
SEGMENT_TIMEOUT = 30  # a stalled read of one playlist or stream segment


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


def fetch(url: str, path: Path | None = None) -> str:
    """One playlist (returned) or stream segment (saved to path)."""
    try:
        with urllib.request.urlopen(url, timeout=SEGMENT_TIMEOUT) as r:
            if not path:
                return r.read().decode("utf-8")
            with path.open("wb") as f:
                shutil.copyfileobj(r, f)
            return ""
    except (OSError, UnicodeDecodeError) as e:  # URLError and timeouts are OSErrors
        raise ClipitError(f"couldn't read the stream: {e}") from e


def fragments(playlist: str, base: str, start: float, end: float) -> tuple[list[str], float] | None:
    """A fragmented-MP4 playlist's URLs for start-end, init segment first, and how far into them start is. None for any
    other playlist. Segment names come from the playlist, so only ones under base are followed."""
    init = re.search(r'#EXT-X-MAP:URI="([^"]+)"', playlist)
    if not init:
        return None
    at, length, parts, first = 0.0, 0.0, [], 0.0
    for line in playlist.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                length = float(line[8:].split(",")[0])
            except ValueError:
                return None
        elif line and not line.startswith("#"):
            if at < end and at + length > start:
                first = first if parts else at
                parts.append(line)
            at += length
    urls = [urllib.parse.urljoin(base, name) for name in (init[1], *parts)]
    if not parts or not all(u.startswith(base) for u in urls):
        return None
    return urls, max(start - first, 0.0)


@functools.lru_cache(maxsize=8)  # a VOD's playlist doesn't change, and every frame and render starts from it
def playlist(url: str) -> str:
    return fetch(url)


@contextmanager
def inputs(urls: list[str], start: float, length: float) -> Iterator[list[str]]:
    """ffmpeg's input arguments for length seconds at start of each stream. ffmpeg can't seek into fragmented-MP4 HLS
    (Twitch's newer VODs): -ss there gives no frames and exit 0, so those parts are fetched to a temp file first."""
    args = []
    with tempfile.TemporaryDirectory(prefix="clipit-") as tmp:
        for i, url in enumerate(urls):
            text = playlist(url) if ".m3u8" in url else ""
            found = fragments(text, url.rsplit("/", 1)[0] + "/", start, start + length)
            if not found:
                args += [*REMOTE, "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", url]
                continue
            part = Path(tmp) / f"{i}.mp4"
            with part.open("wb") as out:
                for n, segment in enumerate(found[0]):
                    piece = Path(tmp) / f"{i}-{n}"
                    fetch(segment, piece)
                    out.write(piece.read_bytes())
                    piece.unlink()
            args += ["-ss", f"{found[1]:.2f}", "-t", f"{length:.2f}", "-i", str(part)]
        yield args
