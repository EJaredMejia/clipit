"""Local speech-to-text with word timestamps, only on the candidate windows."""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np


def transcribe(audio: Path, windows: list[tuple[int, int]], model_name: str) -> Iterator[list[dict]]:
    """Yields the segments of each window, with absolute timestamps."""
    from faster_whisper import WhisperModel  # heavy import, only when needed

    model, lang = WhisperModel(model_name, device="cpu", compute_type="int8"), None
    for a, b in windows:
        pcm = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(a),
                "-t",
                str(b - a),
                "-i",
                str(audio),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "f32le",
                "-",
            ],
            check=True,
            capture_output=True,
            timeout=300,
        ).stdout
        segs, info = model.transcribe(
            np.frombuffer(pcm, np.float32), language=lang, word_timestamps=True, vad_filter=True, condition_on_previous_text=False
        )
        segs = [
            {
                "start": a + s.start,
                "end": a + s.end,
                "text": s.text.strip(),
                "words": [[a + w.start, a + w.end, w.word.strip()] for w in s.words or []],
            }
            for s in segs
        ]
        lang = lang or info.language  # lock language after the first window, screaming confuses detection
        yield segs
