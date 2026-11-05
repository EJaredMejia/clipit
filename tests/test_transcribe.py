import sys
import time
import types

import numpy as np

from clipit import transcribe


def iter_crash():
    raise IndexError("boolean index did not match")  # faster-whisper's find_alignment, mid-iteration
    yield


def test_parallel_windows_keep_order_and_the_first_windows_language(monkeypatch):
    langs = []

    class Model:
        def __init__(self, *a, **kw):
            pass

        def transcribe(self, pcm, language, **kw):
            assert kw["initial_prompt"] == "Chat,"  # keeps "chat" from becoming "Chad"
            langs.append(language)
            a = int(pcm[0])
            if a == 20:
                return iter_crash(), types.SimpleNamespace(language="de")
            time.sleep(0.05 if a == 10 else 0)  # the 2nd window finishes after the 3rd
            seg = types.SimpleNamespace(start=0.0, end=1.0, text=f" w{a} ", words=[])
            return [seg], types.SimpleNamespace(language="de")

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(
        transcribe.subprocess,
        "run",
        lambda cmd, **kw: types.SimpleNamespace(stdout=np.full(4, float(cmd[cmd.index("-ss") + 1]), np.float32).tobytes()),
    )
    out = list(transcribe.transcribe(transcribe.Path("a.m4a"), [(0, 5), (10, 15), (20, 25), (30, 35)], "small"))
    assert [s[0]["text"] if s else None for s in out] == ["w0", "w10", None, "w30"]  # a crashing window is lost alone
    assert [s[0]["start"] for s in out if s] == [0, 10, 30]
    assert langs[0] is None and langs[1:] == ["de", "de", "de"]
