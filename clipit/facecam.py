"""Finding the streamer's webcam overlay so it can go in its own panel."""

import subprocess
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from clipit.util import REMOTE

TOP = (1080, 768)  # facecam panel in the 1080x1920 split layout, gameplay gets the rest


def edge(profile: np.ndarray, lo: int, hi: int, fallback: int) -> int:
    """Strongest line in profile[lo:hi] as a box coordinate, or fallback when nothing stands out."""
    lo, hi = max(0, lo), min(len(profile), hi)
    seg = profile[lo:hi]
    if len(seg) < 3:
        return fallback
    i = int(np.argmax(seg))
    return lo + i + 1 if seg[i] > max(8, 5 * np.median(seg)) else fallback


def near(a: tuple, b: tuple, tol: float) -> bool:
    """Face boxes (frame, x, y, w, h) whose centers are within tol face-sizes of each other."""
    return abs(a[1] + a[3] / 2 - b[1] - b[3] / 2) < tol * a[3] and abs(a[2] + a[4] / 2 - b[2] - b[4] / 2) < tol * a[4]


def cam_faces(boxes: list, n_frames: int, width: int = 960) -> list[tuple[np.ndarray, set[int]]]:
    """Every webcam position: a face that recurs at one spot across the VOD (streamers can have several scene
    layouts, each with the cam elsewhere). Returns (median face box, frames it's in), most frequent first."""
    found, left = [], list(boxes)
    while left:
        group = max(([b for b in left if near(a, b, 0.5)] for a in left), key=lambda g: len({b[0] for b in g}))
        frames = {b[0] for b in group}
        if len(frames) < max(3, n_frames // 8):  # game faces move; a layout used <1/8 of the stream isn't worth it
            break
        face = np.median([b[1:] for b in group], 0)
        if face[2] <= 0.15 * width:  # a huge face is a full-screen camera scene, not an overlay
            found.append((face, frames))
        left = [b for b in left if b not in group]
    return found


def cam_box(
    frames: list[np.ndarray], face: Sequence[float] | np.ndarray, aspect: float = TOP[0] / TOP[1]
) -> tuple[float, float, float, float]:
    """Webcam overlay as (x, y, w, h) frame fractions, snapped to its border (the one edge that's static for hours)."""
    H, W = frames[0].shape
    x, y, w, h = (int(v) for v in face)
    cx, cy = x + w // 2, y + h // 2
    fb = max(0, cx - 2 * w), max(0, cy - 3 * h // 2), min(W, cx + 2 * w), min(H, cy + 3 * h // 2)
    gx = np.mean([np.abs(np.diff(f.astype(np.float32), axis=1)) for f in frames], 0)
    gy = np.mean([np.abs(np.diff(f.astype(np.float32), axis=0)) for f in frames], 0)
    x0, y0, x1, y1 = fb
    for _ in range(2):  # median along the span found so far: the border runs its full length, chat text and room clutter don't
        row = np.median(gy[:, x0:x1], 1)
        y0, y1 = edge(row, y - 3 * h, y, y0), edge(row, y + h, y + 4 * h, y1)
        col = np.median(gx[y0:y1], 0)
        x0, x1 = edge(col, x - 3 * w, x, x0), edge(col, x + w, x + 4 * w, x1)
    if not (1.5 * w <= x1 - x0 <= 8 * w and 1.2 * h <= y1 - y0 <= 6 * h):
        x0, y0, x1, y1 = fb
    bw, bh = x1 - x0, y1 - y0
    if bw / bh > aspect:  # trim to the top panel's shape so nothing stretches
        x0, bw = x0 + (bw - bh * aspect) / 2, bh * aspect
    else:
        y0, bh = y0 + (bh - bw / aspect) / 2, bw / aspect
    return float(x0 / W), float(y0 / H), float(bw / W), float(bh / H)


def grab_frames(url: str, times: Iterable[float]) -> list[np.ndarray]:
    """Gray 960x540 frames at the given seconds, straight from the stream; frames that fail are skipped."""

    def grab(t):
        try:
            raw = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    *REMOTE,
                    "-ss",
                    f"{t:.2f}",
                    "-i",
                    url,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=960:540,format=gray",
                    "-f",
                    "rawvideo",
                    "-",
                ],
                capture_output=True,
                timeout=120,
            ).stdout
        except subprocess.TimeoutExpired:
            return None
        return np.frombuffer(raw, np.uint8).reshape(540, 960) if len(raw) == 960 * 540 else None

    with ThreadPoolExecutor(8) as ex:
        return [f for f in ex.map(grab, times) if f is not None]


def detect_faces(frames: list[np.ndarray]) -> list[tuple]:
    """Frontal faces as (frame index, x, y, w, h) in 960x540 pixels."""
    import cv2  # heavy import, only needed here

    cascades = cv2.data.haarcascades  # pyright: ignore[reportAttributeAccessIssue]  # exists, missing from cv2's stubs
    det = cv2.CascadeClassifier(cascades + "haarcascade_frontalface_default.xml")
    return [(i, *map(int, b)) for i, f in enumerate(frames) for b in det.detectMultiScale(f, 1.1, 6, minSize=(24, 24))]


def find_layouts(url: str, duration: float, n: int = 40) -> list[dict]:
    """Every webcam layout of the VOD as {"face": px box, "box": crop as frame fractions}, from n frames spread
    over the whole stream. Each crop is computed only from the frames that show that layout."""
    frames = grab_frames(url, np.linspace(0.05 * duration, 0.95 * duration, n))
    layouts = []
    for face, idx in cam_faces(detect_faces(frames), len(frames)):
        box = cam_box([frames[i] for i in sorted(idx)], face)
        if at_edge(box):
            layouts.append({"face": [int(v) for v in face], "box": list(box)})
    return layouts


def at_edge(box: Sequence[float], margin: float = 0.05) -> bool:
    """Webcam overlays sit against a frame edge. A recurring face mid-screen is content (e.g. a video being reacted to)."""
    x, y, w, h = box
    return x < margin or y < margin or x + w > 1 - margin or y + h > 1 - margin


def pick_layout(layouts: list[dict], boxes: list[tuple]) -> list[float] | None:
    """The layout whose webcam face shows up in most of a clip's frames (at least 2), else None (blurred layout)."""

    def hits(layout):
        face = (None, *layout["face"])
        return len({b[0] for b in boxes if near(face, b, 0.75)})

    best = max(layouts, key=hits, default=None)
    return best["box"] if best and hits(best) >= 2 else None  # 0 frames (all grabs failed) must not pick a layout blind


def clip_cam(url: str, layouts: list[dict], start: float, end: float, n: int = 5) -> list[float] | None:
    """Crop for one clip: check which of the VOD's webcam layouts is on screen during this clip."""
    if not layouts:
        return None
    frames = grab_frames(url, np.linspace(start, end, n + 2)[1:-1])
    return pick_layout(layouts, detect_faces(frames))
