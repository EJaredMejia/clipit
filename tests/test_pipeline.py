import json

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
        return [{"start": s, "end": s + 20.0, "title": "t", "hook": "h", "description": "d", "hashtags": ["gaming"],
                 "trend": "", "sound": "", "score": 8.0, "why": ""}]  # fmt: skip

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


def test_post_text_is_built_for_every_clip(stages):
    _, out = stages
    work, (clip,) = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out)
    assert clip["caption"] == "d\n\n#gaming" and clip["youtube"] == "d\n\n#gaming #shorts"
    assert "#gaming #shorts" in (work / "posts.md").read_text(encoding="utf-8")


def test_picks_cached_before_descriptions_keep_their_caption(stages):
    _, out = stages
    work, _ = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out)
    old = json.loads((work / "picks.json").read_text(encoding="utf-8"))
    for c in old:
        c["caption"] = "old caption #fyp"
        for k in ("description", "hashtags", "streamer", "youtube"):
            del c[k]
    (work / "picks.json").write_text(json.dumps(old), encoding="utf-8")
    _, (clip,) = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out)
    assert clip["caption"] == "old caption #fyp" and clip["youtube"] == "old caption #fyp\n\n#shorts"
    _, (again,) = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out)
    assert again["caption"] == clip["caption"]  # rerunning on the rewritten cache doesn't stack anything

    old[0]["caption"] = "x" * 2200  # an old caption at TikTok's whole limit leaves no room for the credit
    (work / "picks.json").write_text(json.dumps(old), encoding="utf-8")
    _, (long,) = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out)
    assert long["description"] == "x" * pipeline.ai.TEXT_FIELDS["description"]


def test_streamer_is_only_a_checked_twitch_or_kick_login():
    assert pipeline.streamer({"extractor": "twitch:vod", "uploader_id": "caseoh_"}) == ("caseoh_", "twitch.tv")
    assert pipeline.streamer({"extractor": "kick:vod", "uploader_id": "a-log-burner"}) == ("a-log-burner", "kick.com")
    assert pipeline.streamer({"extractor": "twitch:vod", "uploader_id": "a-log-burner"}) == ("", "")  # no - on Twitch
    assert pipeline.streamer({"extractor": "twitch:vod", "uploader_id": "x y\n#evil"}) == ("", "")
    assert pipeline.streamer({"extractor": "kick:vod", "uploader_id": "x y\n#evil"}) == ("", "")
    assert pipeline.streamer({"extractor": "youtube", "uploader_id": "someone"}) == ("", "")  # not a twitch.tv login
    assert pipeline.streamer({"extractor": "twitch:vod"}) == ("", "")


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


def test_high_effort_edits_each_pick_into_files_of_its_own(stages, monkeypatch):
    _, out = stages
    work, (plain,) = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out)
    polished = []

    def polish(cmd, info, pick, cands, words, loud, folder):
        polished.append(pick)
        return {
            **pick,
            "edit": {"cuts": [], "zooms": [], "sfx": [{"at": pick["start"], "sound": "boom"}], "texts": [], "emphasis": []},
        }

    monkeypatch.setattr(pipeline.edit, "polish", polish)
    pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out, high_effort=True)
    _, (clip,) = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out, high_effort=True)
    assert len(polished) == 1  # the second run used the cached edit
    assert polished[0]["start"] == plain["start"]  # edits the plain pick
    assert clip["file"] != plain["file"] and (work / clip["file"]).exists() and (work / plain["file"]).exists()
    assert "edit" not in json.loads((work / "picks.json").read_text(encoding="utf-8"))[0]  # the plain clips stay as they are
    assert (work / "posts_edited.md").exists()
