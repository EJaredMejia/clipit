import pytest

from clipit import render, util

WORDS = [[10.0, 10.4, "oh"], [10.4, 10.8, "my"], [10.8, 11.2, "god"], [13.0, 13.5, "no"]]


def test_captions_chunk_and_highlight_the_current_word():
    ass = render.make_ass("big {hook} 💀", WORDS, 10.0, 5.0, 768, 1450)
    assert "Hook,,0,0,0,,{\\pos(540,768)}BIG (HOOK)\n" in ass
    assert ass.count("Dialogue: 1,") == 4
    assert "Dialogue: 1,0:00:00.00,0:00:00.40,Cap,,0,0,0,,{\\pos(540,1450)}{\\c&H00FFFF&}OH{\\c&HFFFFFF&} MY GOD" in ass
    assert "Dialogue: 1,0:00:03.00,0:00:03.50,Cap,,0,0,0,,{\\pos(540,1450)}{\\c&H00FFFF&}NO{\\c&HFFFFFF&}" in ass


def test_hook_cannot_inject_subtitle_lines_or_tags():
    ass = render.make_ass("hi\nDialogue: 9,0:00:00.00,9:00:00.00,Cap,,0,0,0,,{\\an5}PWNED\r\n[Script Info]", [], 0, 5, 768, 1450)
    events = ass.split("[Events]")[1].strip().splitlines()
    assert len(events) == 2  # the Format line + exactly one hook event
    assert "{\\an5}" not in ass and ass.count("[Script Info]") == 1


def test_post_texts_credit_the_streamer_then_hashtags():
    clip = {"description": "He did NOT see that coming.", "streamer": "caseoh_", "hashtags": ["CaseOh_", "gaming", "Shorts"]}
    tiktok, youtube = render.post_texts(clip)
    assert tiktok == "He did NOT see that coming.\n\nCredit: twitch.tv/caseoh_\n\n#caseoh_ #gaming #Shorts"
    assert youtube == tiktok  # already has #shorts: not added twice
    tiktok, youtube = render.post_texts({**clip, "hashtags": ["gaming"]})
    assert youtube.endswith("#caseoh_ #gaming #shorts") and "#shorts" not in tiktok


def test_post_texts_credit_kick_streamers_on_kick():
    clip = {"description": "wow", "streamer": "a-log-burner", "site": "kick.com", "hashtags": ["gaming"]}
    tiktok, _ = render.post_texts(clip)
    assert tiktok == "wow\n\nCredit: kick.com/a-log-burner\n\n#alogburner #gaming"  # a - would end the hashtag


def test_post_texts_without_a_known_streamer_have_no_credit():
    tiktok, youtube = render.post_texts({"description": "wow", "streamer": "", "hashtags": []})
    assert (tiktok, youtube) == ("wow", "wow\n\n#shorts")


@pytest.mark.parametrize(
    "streamer", ["lacy", "JasonTheWeen", "silky", "adapt", "yourragegaming", "plaqueboymax", "FaZeRug", "stableronaldo"]
)
@pytest.mark.parametrize("hashtags", [[], ["gaming"], ["StableRonaldo", "gaming"]])
@pytest.mark.parametrize("description", ["A wild moment.", "A wild #StableRonaldo moment."])
def test_faze_descriptions_always_include_ronaldo_once(streamer, hashtags, description):
    clip = {"description": description, "streamer": streamer, "hashtags": hashtags}
    for text in render.post_texts(clip):
        assert text.split().count("#stableronaldo") == 1
        assert text.lower().split().count("#stableronaldo") == 1
        assert f"Credit: twitch.tv/{streamer}" in text


def test_other_streamers_do_not_get_the_faze_hashtag():
    for text in render.post_texts({"description": "wow", "streamer": "caseoh_", "hashtags": ["gaming"]}):
        assert "#stableronaldo" not in text


@pytest.mark.parametrize("streamer", ["kaicenat", "KaiCenat"])
@pytest.mark.parametrize("hashtags", [[], ["gaming"], ["AMP", "rakai", "amp", "RaKai", "gaming"]])
@pytest.mark.parametrize("description", ["A wild moment.", "A wild #AMP #rakai moment."])
def test_kaicenat_descriptions_always_include_amp_and_rakai_once(streamer, hashtags, description):
    for text in render.post_texts({"description": description, "streamer": streamer, "hashtags": hashtags}):
        assert text.split().count("#amp") == 1
        assert text.split().count("#RaKai") == 1
        assert text.lower().split().count("#amp") == 1
        assert text.lower().split().count("#rakai") == 1
        assert f"Credit: twitch.tv/{streamer}" in text


@pytest.mark.parametrize("streamer", ["caseoh_", "lacy", "", "kaicenatfan"])
def test_other_streamers_do_not_get_kaicenat_hashtags(streamer):
    for text in render.post_texts({"description": "wow", "streamer": streamer, "hashtags": ["gaming"]}):
        assert "#amp" not in text.lower()
        assert "#rakai" not in text.lower()


def test_helpers():
    assert util.mmss(3725) == "1:02:05"
    assert util.slug("He RAGE quit!!") == "he-rage-quit"
    assert util.slug("../../etc/passwd") == "etc-passwd"


def test_write_atomic_leaves_no_temp_file(tmp_path):
    p = tmp_path / "x.json"
    util.write_atomic(p, "{}")
    assert p.read_text() == "{}" and list(tmp_path.iterdir()) == [p]
