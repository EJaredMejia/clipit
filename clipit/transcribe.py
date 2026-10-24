"""Local speech-to-text with word timestamps, only on the candidate windows."""

import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

WORKERS = 2  # measured on 12 cores: 2 parallel windows are 1.5x faster, a 3rd adds nothing
# Streamers talk to "chat" constantly; without this Whisper often writes "Chad". One word, so it can't steer the language.
PROMPT = "Chat,"


def transcribe(audio: Path, windows: list[tuple[int, int]], model_name: str) -> Iterator[list[dict]]:
    """Yields the segments of each window, with absolute timestamps."""
    from faster_whisper import WhisperModel  # heavy import, only when needed

    model = WhisperModel(model_name, device="cpu", compute_type="int8", num_workers=WORKERS)

    def one(w: tuple[int, int], lang: str | None) -> tuple[list[dict], str]:
        a, b = w
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
            np.frombuffer(pcm, np.float32),
            language=lang,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=PROMPT,
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
        return segs, info.language

    if not windows:
        return
    segs, lang = one(windows[0], None)  # lock language after the first window, screaming confuses detection
    yield segs
    with ThreadPoolExecutor(WORKERS) as pool:
        for segs, _ in pool.map(lambda w: one(w, lang), windows[1:]):
            yield segs
