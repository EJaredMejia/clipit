import io
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

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


def test_hls_sections_keep_order_and_skip_empty_discontinuities():
    playlist = """#EXTM3U
#EXT-X-DISCONTINUITY-SEQUENCE:0
#EXT-X-DISCONTINUITY
#EXTINF:2.0,
intro.ts
#EXTINF:1.5,
intro2.ts
#EXT-X-DISCONTINUITY
#EXTINF:10,
stream.ts
#EXT-X-DISCONTINUITY
"""
    assert source._hls_sections(playlist) == [(2, 3.5), (3, 10)]


@pytest.mark.parametrize("duration", ["nan", "inf", "-1", "0", "bad"])
def test_hls_rejects_invalid_durations(duration):
    with pytest.raises(ClipitError, match="duration"):
        source._hls_sections(f"#EXTM3U\n#EXTINF:{duration},\na.ts\n")


def test_kick_download_selects_every_nonempty_section_and_caches_only_after_join(monkeypatch, tmp_path):
    manifest = "https://example.test/playlist.m3u8"
    info = {
        "url": manifest,
        "formats": [{"format_id": f"160p-{n}", "format_index": n, "url": manifest} for n in range(4)]
        + [{"format_id": "1080p-2", "format_index": 2, "url": "https://example.test/other.m3u8"}],
    }
    monkeypatch.setattr(source, "sh", lambda *a, **kw: SimpleNamespace(stdout=json.dumps(info)))
    playlist = (
        b"#EXTM3U\n#EXT-X-DISCONTINUITY-SEQUENCE:0\n#EXT-X-DISCONTINUITY\n#EXTINF:2,\na\n#EXT-X-DISCONTINUITY\n#EXTINF:10,\nb\n"
    )
    monkeypatch.setattr(source.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(playlist))

    def download(cmd, stop):
        assert cmd[cmd.index("-f") + 1] == "160p-2,160p-3"
        assert "--abort-on-unavailable-fragments" in cmd
        listing = Path(cmd[cmd.index("--print-to-file") + 2])
        files = [listing.parent / f"part{n}.ts" for n in range(2)]
        for file in files:
            file.write_bytes(b"test media")
        listing.write_text("\n".join(map(str, files)), encoding="utf-8")

    def join(parts, folder, stop):
        assert [duration for _, duration in parts] == [2, 10]
        assert [p.name for p, _ in parts] == ["part0.ts", "part1.ts"]
        assert not (tmp_path / "audio.txt").exists()
        out = folder / "joined.flac"
        out.write_bytes(b"normalized audio")
        return out

    monkeypatch.setattr(source, "_run_audio", download)
    monkeypatch.setattr(source, "_join_audio", join)
    result = source.download_audio("https://kick.com/a/videos/123", tmp_path)
    assert result.read_bytes() == b"normalized audio"
    # A cache hit must not invoke metadata, download, or conversion again.
    monkeypatch.setattr(source, "sh", lambda *a, **kw: pytest.fail("cache was ignored"))
    assert source.download_audio("https://kick.com/a/videos/123", tmp_path) == result


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_join_audio_survives_video_reusing_the_intro_audio_pid(monkeypatch, tmp_path):
    # Kick's intro uses PID 257 for AAC; the stream reuses it for H.264.
    # Raw TS concatenation makes ffmpeg decode video as AAC after the boundary.
    parts = []
    for n, (video_pid, audio_pid) in enumerate(((481, 257), (257, 256))):
        path = tmp_path / f"section{n}.ts"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=size=32x32:rate=10",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={500 + n * 500}:sample_rate=48000",
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-streamid",
                f"0:{video_pid}",
                "-streamid",
                f"1:{audio_pid}",
                "-f",
                "mpegts",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        parts.append((path, 1.0))
    audio = source._join_audio(parts, tmp_path, threading.Event())
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(audio), "-f", "s16le", "-"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert result.stderr == b""
    decoded = result.stdout
    assert len(decoded) == 2 * 16000 * 2
    # Both sections survive; no silence replacing the second section.
    import numpy as np

    samples = np.frombuffer(decoded, dtype=np.int16)
    assert np.std(samples[:16000]) > 1000
    assert np.std(samples[16000:]) > 1000

    def fetch_part(url, to=None):
        assert to is not None
        to.write(Path(url).read_bytes())
        return ""

    monkeypatch.setattr(source, "fetch", fetch_part)
    video = source._kick_video([([str(path)], duration) for path, duration in parts], tmp_path, 3)
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(video), "-map", "0:v", "-map", "0:a", "-f", "null", "-"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert result.stderr == b""


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


def test_fragments_takes_the_ts_segments_a_clip_spans_deep_into_a_long_vod():
    # ffmpeg's own seek stalled for minutes 26 h into a 48 h Twitch VOD made of these, muted ones among them
    ts = "#EXTM3U\n" + "".join(f"#EXTINF:10.000,\n{i}{'-muted' if i == 1 else ''}.ts\n" for i in range(9600))
    assert source.fragments(ts, "https://cdn/v/", 95905.0, 95915.0) == (["https://cdn/v/9590.ts", "https://cdn/v/9591.ts"], 5.0)


def test_fragments_leaves_other_playlists_to_ffmpeg():
    assert source.fragments("#EXTM3U\n#EXTINF:10.000,\nsegment/0\n", "https://cdn/v/", 0, 5) is None


def test_fragments_follows_nothing_outside_the_stream():
    evil = FRAGMENTED.replace("1.mp4", "https://evil.example/1.mp4")
    assert source.fragments(evil, "https://cdn/v/", 12.5, 25.0) is None


def test_a_trickling_segment_stops_at_the_deadline(monkeypatch):
    class Trickle(io.BytesIO):
        def read1(self, size: int | None = -1, /) -> bytes:  # never ends, and never trips the socket timeout
            return b"x"

    monkeypatch.setattr(source.urllib.request, "urlopen", lambda url, timeout: Trickle())
    with pytest.raises(ClipitError, match="stalled"):
        source.fetch("https://example/1.mp4", io.BytesIO(), source.time.monotonic() + 0.05)


def test_inputs_joins_a_fragmented_clip_into_one_local_file(monkeypatch):
    def fake_fetch(url, to=None, deadline=None):
        if to is None:
            return FRAGMENTED
        to.write(url.rsplit("/", 1)[1].encode())
        return ""

    monkeypatch.setattr(source, "fetch", fake_fetch)
    with source.inputs(["https://cdn/v/index.m3u8"], 12.5, 5) as args:
        assert args[:4] == ["-ss", "2.50", "-t", "5.00"]
        assert Path(args[-1]).read_bytes() == b"init-0.mp41.mp4"


def test_inputs_joins_a_ts_clip_into_a_local_indexed_file(monkeypatch):
    def fake_fetch(url, to=None, deadline=None):
        if to is None:
            return "#EXTM3U\n#EXTINF:10.000,\n0.ts\n#EXTINF:10.000,\n1.ts\n#EXT-X-ENDLIST\n"
        to.write(url.rsplit("/", 1)[1].encode())
        return ""

    monkeypatch.setattr(source, "fetch", fake_fetch)
    monkeypatch.setattr(source, "playlist", lambda url: fake_fetch(url))  # not the cached real one
    remuxed = []

    def remux(cmd, timeout):  # a raw TS seek can leave video a second behind the audio: indexed first, like Kick's
        remuxed.append(cmd[cmd.index("-i") + 1])
        Path(cmd[-1]).write_bytes(Path(remuxed[-1]).read_bytes())

    monkeypatch.setattr(source, "sh", remux)
    with source.inputs(["https://cdn/v/index-dvr.m3u8"], 12.5, 5) as args:
        assert args[:4] == ["-ss", "2.50", "-t", "5.00"]  # a local file: no remote seek left to stall
        assert remuxed[0].endswith(".ts") and args[-1].endswith(".mkv") and Path(args[-1]).read_bytes() == b"1.ts"


def test_a_garbled_playlist_is_not_split():
    playlist = '#EXT-X-MAP:URI="init.mp4"\n#EXTINF:oops,\na.mp4\n'
    assert source.fragments(playlist, "https://x/", 0, 10) is None


KICK_PLAYLIST = """#EXTM3U
#EXT-X-DISCONTINUITY-SEQUENCE:0
#EXT-X-DISCONTINUITY
#EXTINF:7,
../../segment/intro
#EXT-X-DISCONTINUITY
#EXTINF:10,
https://stream.kick.com/vod/0.ts
#EXTINF:10,
https://stream.kick.com/vod/1.ts
"""
KICK_MANIFEST = "https://cdn.cloudfront.net/v1/manifest/production-kick-vod/session/0.m3u8"


def test_kick_seek_after_the_intro_does_not_probe_the_intro_pid_table():
    found = source._kick_segments(KICK_PLAYLIST, KICK_MANIFEST, 18, 20)
    assert found == ([(["https://stream.kick.com/vod/1.ts"], 10.0)], 1.0)


def test_kick_clip_crossing_the_intro_keeps_separate_pid_tables():
    found = source._kick_segments(KICK_PLAYLIST, KICK_MANIFEST, 6, 9)
    assert found is not None
    assert len(found[0]) == 2
    assert [duration for _, duration in found[0]] == [7, 10]
    assert found[1] == 6


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://evil.example/0.ts", "http://stream.kick.com/0.ts"])
def test_kick_segments_reject_other_protocols_and_hosts(url):
    with pytest.raises(ClipitError, match="outside"):
        source._kick_segments(KICK_PLAYLIST.replace("https://stream.kick.com/vod/0.ts", url), KICK_MANIFEST, 7, 9)


def test_inputs_fetches_only_the_requested_kick_segment(monkeypatch):
    fetched = []

    def fake_fetch(url, to=None):
        if to is None:
            return KICK_PLAYLIST
        fetched.append(url)
        to.write(b"selected segment")
        return ""

    monkeypatch.setattr(source, "fetch", fake_fetch)

    def remux(cmd, timeout):
        assert cmd[cmd.index("-map") + 1] == "0:v:0?"
        Path(cmd[-1]).write_bytes(Path(cmd[cmd.index("-i") + 1]).read_bytes())

    monkeypatch.setattr(source, "sh", remux)

    source.playlist.cache_clear()
    with source.inputs([KICK_MANIFEST], 18, 2) as args:
        assert args[:4] == ["-ss", "1.00", "-t", "2.00"]
        part = Path(args[-1])
        assert part.read_bytes() == b"selected segment"
    assert not part.exists()
    assert fetched == ["https://stream.kick.com/vod/1.ts"]
    source.playlist.cache_clear()
