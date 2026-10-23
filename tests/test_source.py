import sys
import threading

import pytest

from clipit import source
from clipit.util import ClipitError


def test_stop_kills_a_running_download(monkeypatch, tmp_path):
    monkeypatch.setattr(source, "ytdlp", lambda *a: [sys.executable, "-c", "import time; time.sleep(60)"])
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    with pytest.raises(ClipitError, match="stopped"):
        source.download_audio("https://example/vod", tmp_path, stop)


def test_failed_download_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(source, "ytdlp", lambda *a: [sys.executable, "-c", "raise SystemExit(3)"])
    with pytest.raises(ClipitError, match="exit 3"):
        source.download_audio("https://example/vod", tmp_path)
