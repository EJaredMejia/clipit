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


def test_helpers():
    assert util.mmss(3725) == "1:02:05"
    assert util.slug("He RAGE quit!!") == "he-rage-quit"
    assert util.slug("../../etc/passwd") == "etc-passwd"


def test_write_atomic_leaves_no_temp_file(tmp_path):
    p = tmp_path / "x.json"
    util.write_atomic(p, "{}")
    assert p.read_text() == "{}" and list(tmp_path.iterdir()) == [p]
