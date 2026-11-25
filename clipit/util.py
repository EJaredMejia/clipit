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


class AIDown(ClipitError):
    """Every AI call fails the same way until something changes: a pass stops at once instead of wasting VODs."""


class AILimit(AIDown):
    """The AI CLI's usage limit ran out: it comes back when the limit resets."""


class AILogin(AIDown):
    """The AI CLI isn't logged in, or its login expired: nothing works until the user logs in again."""


# What Claude Code ("You've hit your session limit") and Codex ("You've hit your usage limit") print when out of usage.
# Not "rate limit": a 429 clears in seconds, and the VOD's retry (poll.TRIES) handles it.
LIMIT = re.compile(r"hit your (?:\w+ )?limit|usage limit", re.IGNORECASE)


# What Claude Code ("Not logged in · Please run /login", "Failed to authenticate: OAuth session expired...", "Invalid
# API key") and Codex ("401 Unauthorized") print when their login is gone.
LOGIN = re.compile(
    r"not logged in|please run /login|failed to authenticate|invalid api key|oauth \w+ (?:has )?expired|401 unauthori[sz]ed",
    re.IGNORECASE,
)


def ai_error(message: str, output: str) -> ClipitError:
    """The error for a failed AI call: AILogin or AILimit when its output says the login or the usage ran out."""
    return (AILogin if LOGIN.search(output) else AILimit if LIMIT.search(output) else ClipitError)(message)


def alert(title: str, text: str) -> None:
    """A desktop pop-up for something only the user can fix. Never raises: the log says it too."""
    if sys.platform == "win32":
        flags = 0x30 | 0x1000 | 0x10000  # warning icon, on top of every window, brought to the front
        script = f"import ctypes, sys; ctypes.windll.user32.MessageBoxW(0, sys.argv[1], sys.argv[2], {flags})"
        cmd = [sys.executable, "-c", script, text, title]
    elif sys.platform == "darwin":
        script = ["-e", "on run argv", "-e", "display alert (item 1 of argv) message (item 2 of argv)", "-e", "end run"]
        cmd = ["osascript", *script, title, text]
    else:
        cmd = ["notify-send", "--urgency=critical", title, text]
    try:
        # never waited on, so no timeout: the pop-up stays until the user closes it, while clipit goes on and exits
        kw: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}  # no console window
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except OSError:
        logger.warning("couldn't show a pop-up: %s", title, exc_info=True)


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def ended(vod: dict) -> datetime.datetime:
    """When a VOD's stream ended: publishedAt is when it started."""
    return datetime.datetime.fromisoformat(vod["publishedAt"]) + datetime.timedelta(seconds=vod["lengthSeconds"])


def vod_on(vods: list[dict], day: datetime.date | None) -> dict | None:
    """The newest of vods (newest first, each with publishedAt and lengthSeconds) streaming on day, or the newest one
    when day is None or none was."""
    if day:
        # to noon UTC the next day: an evening stream in the Americas is already past midnight UTC
        lo = datetime.datetime.combine(day, datetime.time(), datetime.UTC)
        hi = lo + datetime.timedelta(hours=36)
        for v in vods:
            if datetime.datetime.fromisoformat(v["publishedAt"]) < hi and ended(v) > lo:
                return v
    return next(iter(vods), None)


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
    """Text for the screen or a post with slurs (n****) and explicit words (b***s) starred out. The clip's audio keeps
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
