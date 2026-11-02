"""Small helpers shared by every stage."""

import datetime
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("clipit")
log = logger.info
# ffmpeg flags for remote inputs: network protocols only, so a hostile playlist can't make ffmpeg read local files.
REMOTE = ["-protocol_whitelist", "https,tls,tcp,crypto,http"]


class ClipitError(Exception):
    """A VOD can't be processed. The CLI prints it; the poller records it and moves on to the next VOD."""


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def sh(cmd: list[str], timeout: float, **kw) -> subprocess.CompletedProcess:
    """Run a command, capture text output, raise on failure or when it runs longer than timeout seconds."""
    return subprocess.run(
        cmd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **kw
    )


def write_atomic(path: Path, data: str | bytes) -> None:
    """Write via a temp file + rename, so a crash never leaves a half-written file that later runs would trust."""
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


SLURS = re.compile(r"\b(?:n[i1]gg\w*|f[a4]gg?\w*|retard\w*|k[iy]ke\w*|chink\w*|spics?|tranny|trannies)\b", re.IGNORECASE)


EXPLICIT = re.compile(
    r"\b(?:boob(?:s|ies)?|tits?|titt(?:y|ies)|nipples?|nips?|penis(?:es)?|dicks?|cocks?|puss(?:y|ies)|vaginas?|cum|porn\w*|"
    r"nudes?|naked|horny)\b",
    re.IGNORECASE,
)


def filter_words(s: str) -> str:
    """Text for the screen or a post with slurs (n****) and explicit words (b**bs) starred out. The clip's audio keeps
    them: the user chose text only, and explicit titles risk an age limit on YouTube."""
    s = SLURS.sub(lambda m: m[0][0] + "*" * (len(m[0]) - 1), s)
    return EXPLICIT.sub(lambda m: m[0][0] + "*" * (len(m[0]) - 2) + m[0][-1], s)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "clip"


def mmss(x: float) -> str:
    x = int(x)
    return f"{x // 3600}:{x % 3600 // 60:02d}:{x % 60:02d}"


def ytdlp(*args: str) -> list[str]:
    return [sys.executable, "-m", "yt_dlp", "--no-warnings", *args]
