import sys
import threading
from pathlib import Path

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


FRAGMENTED = """#EXTM3U
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:10.000,
0.mp4
#EXTINF:10.000,
1.mp4
#EXTINF:10.000,
2.mp4
#EXTINF:10.000,
3.mp4
#EXT-X-ENDLIST
"""


def test_fragments_takes_the_init_and_the_segments_a_clip_spans():
    parts = ["https://cdn/v/init-0.mp4", "https://cdn/v/1.mp4", "https://cdn/v/2.mp4"]
    assert source.fragments(FRAGMENTED, "https://cdn/v/", 12.5, 25.0) == (parts, 2.5)


def test_fragments_leaves_other_playlists_to_ffmpeg():
    assert source.fragments("#EXTM3U\n#EXTINF:10.000,\n0.ts\n", "https://cdn/v/", 0, 5) is None


def test_fragments_follows_nothing_outside_the_stream():
    evil = FRAGMENTED.replace("1.mp4", "https://evil.example/1.mp4")
    assert source.fragments(evil, "https://cdn/v/", 12.5, 25.0) is None


def test_inputs_joins_a_fragmented_clip_into_one_local_file(monkeypatch):
    def fake_fetch(url, path=None):
        if path is None:
            return FRAGMENTED
        path.write_bytes(url.rsplit("/", 1)[1].encode())
        return ""

    monkeypatch.setattr(source, "fetch", fake_fetch)
    with source.inputs(["https://cdn/v/index.m3u8"], 12.5, 5) as args:
        assert args[:4] == ["-ss", "2.50", "-t", "5.00"]
        assert Path(args[-1]).read_bytes() == b"init-0.mp41.mp4"
