from clipit import pipeline


def test_clip_file_changes_with_the_time_range():
    clip = {"title": "Huge Clutch!", "start": 12.0, "end": 40.5}
    assert pipeline.clip_file(1, clip) == "clips/01_huge-clutch_12.0-40.5.mp4"
    # same rank and title, different moment: must not hit the old render's file
    assert pipeline.clip_file(1, {**clip, "start": 13.0}) != pipeline.clip_file(1, clip)
