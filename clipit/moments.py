"""Finding candidate moments: per-second score from loudness and chat, then the top peaks."""

import re
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from clipit.util import ClipitError

HYPE = re.compile(r"\b(lulw?|kekw|omegalul|lmf?ao+|lo+l|icant|clip|pog\w*|huh|w+|l+)\b|\?{3,}|💀|😂|🤣", re.I)
CHAT_LAG = 8  # chat reacts a few seconds after the moment
WINDOW = (60, 30)  # seconds before/after a peak that the AI gets to choose from
SR = 4000  # loudness only needs a coarse sample rate
DECODE_TIMEOUT = 1800


def loudness(audio: Path) -> np.ndarray:
    """Per-second loudness in dB, streamed so multi-hour VODs don't need GBs of RAM."""
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], stdout=subprocess.PIPE
    )
    watchdog = threading.Timer(DECODE_TIMEOUT, p.kill)  # the read loop blocks, so a hung ffmpeg must be killed from outside
    watchdog.start()
    db = []
    try:
        while chunk := p.stdout.read(SR * 2 * 600):  # pyright: ignore[reportOptionalMemberAccess]  # stdout=PIPE sets it
            x = np.frombuffer(chunk[: len(chunk) // (SR * 2) * SR * 2], np.int16).astype(np.float32).reshape(-1, SR)
            db.extend(10 * np.log10((x**2).mean(1) + 1))
    finally:
        watchdog.cancel()
    if p.wait():
        raise ClipitError(f"ffmpeg could not read {audio} (or took over {DECODE_TIMEOUT}s)")
    return np.array(db)


def smooth(x: np.ndarray, w: int) -> np.ndarray:
    return np.convolve(x, np.ones(w) / w, "same")


def z(x: np.ndarray) -> np.ndarray:
    return (x - x.mean()) / (x.std() + 1e-9)


def signals(duration: float, loud: np.ndarray, chat: list) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-second virality score from loudness and (lag-corrected, hype-weighted) chat rate."""
    n = int(duration)
    db = np.zeros(n)
    db[: min(n, len(loud))] = loud[:n]
    c = np.zeros(n)
    for t, msg, *weight in chat:  # Kick's sampled chat carries a weight, see kick.fetch_chat
        if t < n:
            c[int(t)] += (2 if HYPE.search(msg) else 1) * (weight[0] if weight else 1)
    score = z(smooth(db, 5))
    if c.any():
        score = 0.5 * score + z(np.roll(smooth(c, 15), -CHAT_LAG))
    return score, db, c


def peaks(score: np.ndarray, k: int, gap: int, taken: Sequence[int] = ()) -> list[int]:
    """The highest seconds at least gap apart, in time order, that fill up to k slots around the ones already taken."""
    picked = list(taken)
    for t in np.argsort(score)[::-1]:
        if len(picked) >= k:
            break
        if all(abs(t - p) >= gap for p in picked):
            picked.append(int(t))
    return sorted(picked[len(taken) :])


def windows(score: np.ndarray, k: int, duration: float, clipped: Sequence[tuple[int, int]] = ()) -> list[tuple[int, int]]:
    """The k best (start, end) windows, never overlapping: first the moments people clipped ((second, views), a clip
    starts just before its moment), then the peaks."""
    gap = sum(WINDOW) + 30
    points: list[int] = []
    for t in (t + WINDOW[0] // 2 for t, _ in clipped):
        if 0 <= t < duration and len(points) < k and all(abs(t - p) >= gap for p in points):
            points.append(int(t))
    return [(max(0, t - WINDOW[0]), min(int(duration), t + WINDOW[1])) for t in (*points, *peaks(score, k, gap, points))]
