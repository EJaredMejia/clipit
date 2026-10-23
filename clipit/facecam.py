"""Finding the streamer's webcam overlay so it can go in its own panel."""

import subprocess
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


def cam_face(boxes: list, n_frames: int, width: int = 960) -> np.ndarray | None:
    """Median (x, y, w, h) of the face seen at the same spot in many frames (the webcam), else None."""
    best = []
    for _, x, y, w, h in boxes:
        near = [b for b in boxes if abs(b[1] + b[3] / 2 - x - w / 2) < 1.5 * w and abs(b[2] + b[4] / 2 - y - h / 2) < 1.5 * h]
        if len({b[0] for b in near}) > len({b[0] for b in best}):
            best = near
    if len({b[0] for b in best}) < max(3, n_frames // 4):
        return None
    face = np.median([b[1:] for b in best], 0)
    return None if face[2] > 0.15 * width else face  # a huge face is a full-screen camera scene, not an overlay


def cam_box(frames: list[np.ndarray], face, aspect: float = TOP[0] / TOP[1]) -> tuple[float, float, float, float]:
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


def find_facecam(url: str, duration: float, n: int = 40) -> tuple[float, float, float, float] | None:
    """Webcam overlay as frame fractions, or None. Votes over frames from the whole VOD: the webcam stays put, game faces move."""
    import cv2  # only needed here

    def grab(t):
        try:
            raw = subprocess.run(["ffmpeg", "-v", "error", *REMOTE, "-ss", f"{t:.2f}", "-i", url, "-frames:v", "1",
                                  "-vf", "scale=960:540,format=gray", "-f", "rawvideo", "-"],
                                 capture_output=True, timeout=120).stdout
        except subprocess.TimeoutExpired:
            return None
        return np.frombuffer(raw, np.uint8).reshape(540, 960) if len(raw) == 960 * 540 else None

    with ThreadPoolExecutor(8) as ex:
        frames = [f for f in ex.map(grab, np.linspace(0.05 * duration, 0.95 * duration, n)) if f is not None]
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    boxes = [(i, *b) for i, f in enumerate(frames) for b in det.detectMultiScale(f, 1.1, 6, minSize=(24, 24))]
    face = cam_face(boxes, len(frames))
    return None if face is None else cam_box(frames, face)
