"""Talking to the video site through yt-dlp: metadata, audio-only download, stream URLs to seek into."""

import functools
import json
import math
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse

from clipit.util import REMOTE, ClipitError, log, sh, write_atomic, ytdlp

DOWNLOAD_TIMEOUT = 3 * 3600  # a 48 h VOD's audio is ~2.5 GB
SEGMENT_TIMEOUT = 30  # a stalled read of one playlist or stream segment
FETCH_TIMEOUT = 90  # all of one clip's segments: a stream trickling in never trips SEGMENT_TIMEOUT


def _run_audio(cmd: list[str], stop: threading.Event, timeout: float = DOWNLOAD_TIMEOUT) -> None:
    """Downloads and local conversions must both stop when the parallel chat fetch fails."""
    if stop.is_set():
        raise ClipitError("audio download stopped")
    p = subprocess.Popen(cmd)
    deadline = time.monotonic() + timeout
    try:
        while p.poll() is None:
            if stop.wait(0.1) or time.monotonic() > deadline:
                raise ClipitError("audio download " + ("stopped" if stop.is_set() else f"took over {timeout}s"))
        if p.returncode:
            raise ClipitError(f"{Path(cmd[0]).stem} could not prepare the audio (exit {p.returncode})")
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()


def _hls_sections(playlist: str) -> list[tuple[int, float]]:
    """Nonempty yt-dlp format indices and their durations, in playlist order."""
    sections: dict[int, float] = {}
    index, duration = 0, None
    for line in playlist.splitlines():
        line = line.strip()
        # Match yt-dlp's split numbering, which also counts DISCONTINUITY-SEQUENCE.
        if line.startswith("#EXT-X-DISCONTINUITY"):
            index += 1
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line[8:].split(",")[0])
            except ValueError as e:
                raise ClipitError("Kick returned an invalid HLS segment duration") from e
            if not math.isfinite(duration) or duration <= 0:
                raise ClipitError("Kick returned an invalid HLS segment duration")
        elif line and not line.startswith("#"):
            if duration is None:
                raise ClipitError("Kick returned an HLS segment without a duration")
            sections[index] = sections.get(index, 0) + duration
            duration = None
    if not sections or sum(sections.values()) > 48 * 3600 + 60 or len(sections) > 256:
        raise ClipitError("Kick returned an empty or oversized HLS playlist")
    return list(sections.items())


def _join_audio(parts: list[tuple[Path, float]], folder: Path, stop: threading.Event) -> Path:
    """Reset the demuxer at every HLS discontinuity; normalize before concatenating."""
    for n, (part, duration) in enumerate(parts):
        _run_audio(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-xerror",
                "-i",
                str(part),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-af",
                f"apad=whole_dur={duration:.6f}",
                "-t",
                f"{duration:.6f}",
                str(folder / f"{n}.flac"),
            ],
            stop,
            timeout=1800,
        )
    listing = folder / "concat.txt"
    write_atomic(listing, "".join(f"file '{n}.flac'\n" for n in range(len(parts))))
    out = folder / "joined.flac"
    _run_audio(
        # FLAC frames contain sample positions: re-encode so the next section does not restart at zero.
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "concat", "-safe", "1", "-i", str(listing), "-c:a", "flac", str(out)],
        stop,
        timeout=1800,
    )
    return out


def _kick_audio(url: str, work: Path, stop: threading.Event) -> Path:
    """Kick's intro and stream can reuse the same TS PID for different codecs."""
    info = json.loads(sh(ytdlp("-J", "--hls-split-discontinuity", "-f", "ba/worst", url), timeout=120).stdout)
    manifest = str(info.get("url", ""))
    if urlparse(manifest).scheme not in ("http", "https"):
        raise ClipitError("Kick returned an invalid HLS URL")
    request = urllib.request.Request(manifest, headers=info.get("http_headers") or {})
    try:
        with urllib.request.urlopen(request, timeout=SEGMENT_TIMEOUT) as response:
            playlist = response.read(8 * 1024 * 1024 + 1)
    except OSError as e:
        raise ClipitError(f"couldn't read Kick's audio playlist: {e}") from e
    if len(playlist) > 8 * 1024 * 1024:
        raise ClipitError("Kick returned an oversized HLS playlist")
    sections = _hls_sections(playlist.decode("utf-8"))
    formats = {f.get("format_index"): f["format_id"] for f in info["formats"] if f.get("url") == manifest}
    try:
        selected = [formats[index] for index, _ in sections]
    except KeyError as e:
        raise ClipitError("Kick's HLS sections do not match yt-dlp's formats") from e
    log("downloading %s Kick audio section(s) separately", len(sections))
    with tempfile.TemporaryDirectory(prefix="audio-", dir=work) as tmp:
        folder = Path(tmp).resolve()
        metadata, listing = folder / "info.json", folder / "paths.txt"
        write_atomic(metadata, json.dumps(info))
        _run_audio(
            ytdlp(
                "--load-info-json",
                str(metadata),
                "-N",
                "32",
                "--abort-on-unavailable-fragments",
                "--fixup",
                "never",
                "--no-progress",
                "-f",
                ",".join(selected),
                "--print-to-file",
                "after_move:filepath",
                str(listing),
                "-o",
                str(folder / "part.%(format_id)s.%(ext)s"),
            ),
            stop,
        )
        paths = listing.read_text(encoding="utf-8").splitlines()
        if len(paths) != len(sections):
            raise ClipitError("Kick's audio download did not return every HLS section")
        parts = [(Path(path), duration) for path, (_, duration) in zip(paths, sections, strict=True)]
        if any(path.resolve().parent != folder for path, _ in parts):
            raise ClipitError("Kick's audio download returned a path outside its temporary folder")
        joined = _join_audio(parts, folder, stop)
        audio = work / "audio.flac"
        joined.replace(audio)
        write_atomic(work / "audio.txt", str(audio.resolve()))
    return audio


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
        stop = stop or threading.Event()
        if urlparse(url).hostname in ("kick.com", "www.kick.com"):
            return _kick_audio(url, work, stop)
        _run_audio(
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
            ),
            stop,
        )
    return Path(done.read_text(encoding="utf-8").strip().splitlines()[-1])


def stream_urls(url: str) -> list[str]:
    """Direct 1080p URL(s), video first. Signed by Twitch for hours, so fetch fresh per run."""
    return sh(ytdlp("-g", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", url), timeout=120).stdout.split()


def fetch(url: str, to: BinaryIO | None = None, deadline: float = math.inf) -> str:
    """One playlist (returned) or stream segment (written to the open file to, by the time.monotonic() deadline)."""
    try:
        # Kick's segment CDN rejects urllib's default User-Agent with HTTP 403.
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=SEGMENT_TIMEOUT) as r:
            if not to:
                return r.read().decode("utf-8")
            while chunk := r.read1(64 * 1024):
                if time.monotonic() > deadline:
                    raise ClipitError("couldn't read the stream: it stalled")
                to.write(chunk)
            return ""
    except (OSError, UnicodeDecodeError) as e:  # URLError and timeouts are OSErrors
        raise ClipitError(f"couldn't read the stream: {e}") from e


def fragments(playlist: str, base: str, start: float, end: float) -> tuple[list[str], float] | None:
    """A Twitch playlist's URLs for start-end (a fragmented-MP4 one's init segment first), and how far into them start
    is. None for any other playlist. Segment names come from the playlist, so only ones under base are followed.
    MPEG-TS ones too: ffmpeg's own HLS seek stalled for minutes 26 h into a 48 h VOD, where these segments read fine."""
    init = re.search(r'#EXT-X-MAP:URI="([^"]+)"', playlist)
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
    if not init and not all(urllib.parse.urlsplit(name).path.endswith(".ts") for name in parts):
        return None
    urls = [urllib.parse.urljoin(base, name) for name in (*([init[1]] if init else []), *parts)]
    if not parts or not all(u.startswith(base) for u in urls):
        return None
    return urls, max(start - first, 0.0)


@functools.lru_cache(maxsize=8)  # a VOD's playlist doesn't change, and every frame and render starts from it
def playlist(url: str) -> str:
    return fetch(url)


def _kick_segments(text: str, url: str, start: float, end: float) -> tuple[list[tuple[list[str], float]], float] | None:
    """Read only a clip's TS segments, keeping the intro's PID table out of later seeks."""
    host = urlparse(url).hostname or ""
    if not host.endswith(".cloudfront.net") or "/production-kick-vod/" not in url or "#EXT-X-MAP:" in text:
        return None
    _hls_sections(text)  # validate every duration before using it in the timeline
    at, length, first, index = 0.0, 0.0, None, 0
    groups: dict[int, tuple[list[str], float]] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-DISCONTINUITY"):
            index += 1
        elif line.startswith("#EXTINF:"):
            length = float(line[8:].split(",")[0])
        elif line and not line.startswith("#"):
            if at < end and at + length > start:
                segment = urllib.parse.urljoin(url, line)
                parsed = urlparse(segment)
                if parsed.scheme != "https" or parsed.hostname not in (host, "stream.kick.com"):
                    raise ClipitError("Kick's playlist points outside its segment CDNs")
                first = at if first is None else first
                urls, duration = groups.get(index, ([], 0.0))
                groups[index] = ([*urls, segment], duration + length)
            at += length
    if first is None:
        raise ClipitError("Kick's playlist has no segments at the requested clip time")
    return list(groups.values()), max(start - first, 0.0)


def _indexed(raw: Path, out: Path) -> Path:
    """Index a downloaded TS before seeking: a raw TS seek can skip to the next keyframe, leaving video a second behind
    the audio. Also standardizes stream order."""
    sh(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", str(out)], timeout=120)
    return out


def _kick_video(groups: list[tuple[list[str], float]], folder: Path, i: int) -> Path:
    files = []
    for n, (urls, duration) in enumerate(groups):
        raw = folder / f"{i}-{n}.ts"
        with raw.open("wb") as out:
            for segment in urls:
                fetch(segment, out)
        normalized = _indexed(raw, folder / f"{i}-{n}-normalized.mkv")
        if len(groups) == 1:
            return normalized
        files.append(f"file '{normalized.name}'\nduration {duration:.6f}\n")
    listing = folder / f"{i}-concat.txt"
    write_atomic(listing, "".join(files))
    joined = folder / f"{i}-joined.mkv"
    sh(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(listing), "-c", "copy", str(joined)], timeout=120)
    return joined


@contextmanager
def inputs(urls: list[str], start: float, length: float) -> Iterator[list[str]]:
    """ffmpeg's input arguments for length seconds at start of each stream. ffmpeg can't seek into fragmented-MP4 HLS
    (Twitch's newer VODs): -ss there gives no frames and exit 0, and stalls deep into long MPEG-TS ones, so Twitch
    parts are fetched to a temp file first."""
    args = []
    with tempfile.TemporaryDirectory(prefix="clipit-") as tmp:
        for i, url in enumerate(urls):
            text = playlist(url) if ".m3u8" in url else ""
            kick = _kick_segments(text, url, start, start + length)
            if kick:
                part = _kick_video(kick[0], Path(tmp), i)
                args += ["-ss", f"{kick[1]:.2f}", "-t", f"{length:.2f}", "-i", str(part)]
                continue
            found = fragments(text, url.rsplit("/", 1)[0] + "/", start, start + length)
            if not found:
                args += [*REMOTE, "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", url]
                continue
            ts = urllib.parse.urlsplit(found[0][0]).path.endswith(".ts")  # no init segment: MPEG-TS, see fragments
            part = Path(tmp) / f"{i}{'.ts' if ts else '.mp4'}"
            deadline = time.monotonic() + FETCH_TIMEOUT
            with part.open("wb") as out:
                for segment in found[0]:
                    fetch(segment, out, deadline)
            if ts:
                part = _indexed(part, Path(tmp) / f"{i}.mkv")
            args += ["-ss", f"{found[1]:.2f}", "-t", f"{length:.2f}", "-i", str(part)]
        yield args
