import numpy as np
import pytest

from clipit import pipeline


@pytest.fixture
def stages(tmp_path, monkeypatch):
    """Every external stage of process_vod faked; counts audio downloads."""
    downloads = []

    def download(url, work, stop=None):
        downloads.append(url)
        return work / "audio.m4a"

    def transcribe(audio, windows, model):
        for a, _ in windows:
            yield [{"start": a, "end": a + 2.0, "text": "hi", "words": [[a, a + 0.5, "hi"]]}]

    def pick(cmd, info, cands, n, work):
        s = cands[0]["start"]
        return [{"start": s, "end": s + 20.0, "title": "t", "hook": "h", "caption": "c", "trend": "", "sound": "",
                 "score": 8.0, "why": ""}]  # fmt: skip

    loud = np.full(600, 50.0)
    loud[300] = 90.0
    monkeypatch.setattr(pipeline.source, "fetch_info", lambda url: {"id": "v9", "duration": 600.0, "extractor": "youtube"})
    monkeypatch.setattr(pipeline.source, "download_audio", download)
    monkeypatch.setattr(pipeline.source, "stream_urls", lambda url: ["https://example/v.m3u8"])
    monkeypatch.setattr(pipeline.moments, "loudness", lambda audio: loud)
    monkeypatch.setattr(pipeline.transcribe, "transcribe", transcribe)
    monkeypatch.setattr(pipeline.ai, "pick_clips", pick)
    monkeypatch.setattr(pipeline.facecam, "find_layouts", lambda url, dur: [])
    monkeypatch.setattr(pipeline.facecam, "clip_cam", lambda *a: None)
    monkeypatch.setattr(pipeline.render, "render", lambda urls, clip, words, out, cam: out.write_bytes(b"mp4"))
    return downloads, tmp_path


def test_audio_is_downloaded_once_and_only_when_a_stage_needs_it(stages):
    downloads, out = stages
    work, clips = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out)
    assert len(downloads) == 1  # loudness and transcription share one download
    assert len(clips) == 1 and (work / clips[0]["file"]).exists()

    pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out)
    assert len(downloads) == 1  # everything cached: no download at all

    (work / "transcripts.json").unlink()  # partly cached: loudness kept, transcripts missing
    pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out)
    assert len(downloads) == 2


def test_clip_file_changes_with_the_time_range():
    clip = {"title": "Huge Clutch!", "start": 12.0, "end": 40.5}
    assert pipeline.clip_file(1, clip) == "clips/01_huge-clutch_12.0-40.5.mp4"
    # same rank and title, different moment: must not hit the old render's file
    assert pipeline.clip_file(1, {**clip, "start": 13.0}) != pipeline.clip_file(1, clip)


def test_chat_failure_stops_the_audio_download(stages, monkeypatch):
    _, out = stages
    stopped = []

    def download(url, work, stop):
        stopped.append(stop.wait(5))  # a real download runs for minutes; returns as soon as it's told to stop
        return work / "audio.m4a"

    def chat(info):
        raise pipeline.ClipitError("chat broke")

    monkeypatch.setattr(pipeline.source, "download_audio", download)
    monkeypatch.setattr(pipeline, "get_chat", chat)
    with pytest.raises(pipeline.ClipitError, match="chat broke"):
        pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out)
    assert stopped == [True]
