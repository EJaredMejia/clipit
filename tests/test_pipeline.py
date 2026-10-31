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
    monkeypatch.setattr(pipeline.ai, "veto", lambda *a: [])
    monkeypatch.setattr(pipeline.render, "sheets", lambda *a: [])
    monkeypatch.setattr(pipeline.facecam, "clip_cam", lambda *a: None)
    monkeypatch.setattr(pipeline.facecam, "clip_face", lambda *a: None)
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

    def polish(cmd, info, pick, cands, words, loud, folder, look):
        polished.append(pick)
        events.append("edited")
        return {
            **pick,
            "edit": {"cuts": [], "zooms": [], "sfx": [{"at": pick["start"], "sound": "boom"}], "texts": [], "emphasis": []},
        }

    events = []
    monkeypatch.setattr(pipeline.source, "stream_urls", lambda url: events.append("links") or ["https://example/v.m3u8"])
    monkeypatch.setattr(pipeline.edit, "polish", polish)
    pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out, high_effort=True)
    assert events[-2:] == ["edited", "links"]  # the clips render from links fetched after the editing
    _, (clip,) = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out, high_effort=True)
    assert len(polished) == 1  # the second run used the cached edit
    assert polished[0]["start"] == plain["start"]  # edits the plain pick
    assert clip["file"] != plain["file"] and (work / clip["file"]).exists() and (work / plain["file"]).exists()
    assert "edit" not in json.loads((work / "picks.json").read_text(encoding="utf-8"))[0]  # the plain clips stay as they are
    assert (work / "posts_edited.md").exists()


def test_picks_scored_under_min_score_are_dropped_before_rendering(stages, monkeypatch):
    _, out = stages
    picked = pipeline.ai.pick_clips

    def two(*a):
        (c,) = picked(*a)
        return [c, {**c, "start": c["start"] + 30, "end": c["end"] + 30, "score": pipeline.MIN_SCORE - 1}]

    monkeypatch.setattr(pipeline.ai, "pick_clips", two)
    work, clips = pipeline.process_vod("https://example/vod", n_clips=2, ai_cmd="ai", out_root=out)
    assert [c["score"] for c in clips] == [8.0]
    weak = {"variant": "plain", "start_s": clips[0]["start"] + 30, "end_s": clips[0]["end"] + 30}
    assert pipeline.made_as(work, weak)["score"] == pipeline.MIN_SCORE - 1  # an old kept clip of it can still be re-edited

    monkeypatch.setattr(pipeline.ai, "pick_clips", lambda *a: [{**c, "score": 1.0} for c in picked(*a)])
    monkeypatch.setattr(pipeline.source, "stream_urls", lambda url: pytest.fail("fetched the video with nothing to render"))
    _, clips = pipeline.process_vod("https://example/vod2", n_clips=1, ai_cmd="ai", out_root=out / "b")
    assert clips == []  # a VOD with only trash picks is done, not failed


def test_high_effort_leaves_low_scored_picks_plain(stages, monkeypatch):
    _, out = stages
    picked = pipeline.ai.pick_clips

    def weak(*a):
        return [{**c, "score": pipeline.db.AUTO_SCORE - 1} for c in picked(*a)]

    monkeypatch.setattr(pipeline.ai, "pick_clips", weak)
    monkeypatch.setattr(pipeline.edit, "polish", lambda *a: pytest.fail("a weak pick was sent to the AI editor"))
    _, (clip,) = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out, high_effort=True)
    assert "edit" not in clip and not clip["file"].endswith("_edited.mp4")


def test_reedit_starts_from_how_the_ai_made_the_clip_and_renders_a_new_video(stages, monkeypatch):
    _, out = stages
    work, (plain,) = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=out)
    asked = []

    def polish(cmd, info, pick, cands, words, loud, folder, request, at, look):
        asked.append((pick["title"], request, at))
        return {**pick, "edit": {"cuts": [], "zooms": [], "sfx": [{"at": at, "sound": "boom"}], "texts": [], "emphasis": []}}

    monkeypatch.setattr(pipeline.edit, "polish", polish)
    # a clip saved before the catalog kept how the AI made it: found in the VOD's picks.json
    clip = {"id": 1, "vod_id": "v9", "idx": 1, "start_s": plain["start"], "end_s": plain["end"], "variant": "plain", "pick": None}
    new = pipeline.reedit("https://example/vod", clip, "a boom", 2.0, "ai", out)
    assert asked == [("t", "a boom", plain["start"] + 2.0)]  # the marked second, in the VOD
    assert new["file"].startswith("v9/clips/") and (out / new["file"]).exists() and new["file"] != f"v9/{plain['file']}"
    (work / "transcripts.json").unlink()
    with pytest.raises(pipeline.ClipitError, match="deleted"):
        pipeline.reedit("https://example/vod", clip, "a boom", None, "ai", out)


def test_preview_fetches_fresh_links_and_skips_a_stream_picture_ffmpeg_didnt_write(monkeypatch, tmp_path):
    fetched, shot = [], tmp_path / "frame01.jpg"
    monkeypatch.setattr(pipeline.source, "stream_urls", lambda url: fetched.append(url) or ["https://v", "https://a"])
    monkeypatch.setattr(pipeline.render, "source_frame", lambda url, t, path: None)  # exits fine, writes nothing
    monkeypatch.setattr(pipeline, "render_clip", lambda *a: None)
    monkeypatch.setattr(pipeline.render, "frames", lambda video, times, folder: [(0.0, shot)])
    look = pipeline.preview("https://example/vod", [], [])
    clip = {"start": 10.0, "end": 30.0}
    assert look(clip, tmp_path / "r1") == [("0.0 s", shot)]
    look(clip, tmp_path / "r2")
    assert len(fetched) == 2  # every round renders from links fetched for it


def test_a_clip_the_review_stops_never_reaches_the_auto_post_score(stages, monkeypatch):
    _, tmp_path = stages
    monkeypatch.setattr(pipeline.ai, "veto", lambda *a: ["visible: static screen"])
    _, clips = pipeline.process_vod("https://example/vod", n_clips=1, ai_cmd="ai", out_root=tmp_path)
    assert clips[0]["score"] < pipeline.db.AUTO_SCORE
    assert "static screen" in clips[0]["why"]


def test_a_rerun_doesnt_review_a_reviewed_clip_or_look_at_weak_ones(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline.ai, "veto", lambda *a: pytest.fail("reviewed again"))
    for clip in ({"score": 9.0, "reviewed": True}, {"score": 7.0}):
        pipeline.review("x", {}, clip, [], tmp_path)


def test_no_webcam_but_a_person_in_shot_crops_on_them_unless_the_edit_chose(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(pipeline.facecam, "clip_cam", lambda *a: None)
    monkeypatch.setattr(pipeline.facecam, "clip_face", lambda *a: 0.3)
    monkeypatch.setattr(pipeline.render, "render", lambda urls, clip, words, out, cam: seen.append(clip.get("edit")))
    pipeline.render_clip(["u"], [], {"start": 0.0, "end": 20.0}, [], tmp_path / "a.mp4")
    pipeline.render_clip(["u"], [], {"start": 0.0, "end": 20.0, "edit": {"layout": "fill"}}, [], tmp_path / "b.mp4")
    assert seen == [{"layout": "crop", "focus": 0.3}, {"layout": "fill"}]


def test_one_clip_whose_stream_cant_be_read_doesnt_lose_the_vod(stages, monkeypatch):
    _, tmp_path = stages

    def broken(urls, clip, words, out, cam):
        if clip["hook"] == "h":
            raise pipeline.ClipitError("couldn't read the stream")
        out.write_bytes(b"mp4")

    def two(cmd, info, cands, n, work):
        base = {"title": "t", "description": "d", "hashtags": ["a"], "trend": "", "sound": "", "score": 6.0, "why": ""}
        s = cands[0]["start"]
        return [{**base, "start": s, "end": s + 20.0, "hook": "h"}, {**base, "start": s + 1, "end": s + 21.0, "hook": "ok"}]

    monkeypatch.setattr(pipeline.ai, "pick_clips", two)
    monkeypatch.setattr(pipeline.render, "render", broken)
    _, clips = pipeline.process_vod("https://example/vod", n_clips=2, ai_cmd="ai", out_root=tmp_path)
    assert [c["hook"] for c in clips] == ["ok"]


def test_a_review_that_never_happened_is_asked_again(monkeypatch, tmp_path):
    answers = [["review: failed"], []]
    monkeypatch.setattr(pipeline.render, "sheets", lambda *a: [])
    monkeypatch.setattr(pipeline.ai, "veto", lambda *a: answers.pop(0))
    clip = {"score": 9.0, "start": 0.0, "end": 20.0, "file": "c.mp4", "why": ""}
    pipeline.review("x", {}, clip, [], tmp_path)
    assert clip["score"] < pipeline.db.AUTO_SCORE
    pipeline.review("x", {}, clip, [], tmp_path)
    assert clip["score"] == 9.0 and clip["reviewed"]


def test_cuts_count_against_the_review_length(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(pipeline.render, "sheets", lambda *a: [])
    monkeypatch.setattr(pipeline.ai, "veto", lambda *a: seen.append(a[-1]) or [])
    edit = {"cuts": [{"start": 10.0, "end": 30.0}]}
    pipeline.review("x", {}, {"score": 9.0, "start": 0.0, "end": 50.0, "file": "c.mp4", "edit": edit}, [], tmp_path)
    assert seen == [30.0]
