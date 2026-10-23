import numpy as np

from clipit import moments


def test_peaks_highest_first_and_spaced():
    s = np.zeros(1000)
    s[100], s[105], s[500] = 5, 4, 3
    assert moments.peaks(s, 2, gap=60) == [100, 500]


def test_hype_words():
    assert moments.HYPE.search("KEKW") and moments.HYPE.search("W") and moments.HYPE.search("???")
    assert not moments.HYPE.search("hello well")


def test_chat_spike_is_shifted_earlier_by_chat_lag():
    score, _, rate = moments.signals(300, np.full(300, 50.0), [[150, "KEKW"]] * 40)
    assert score[150 - moments.CHAT_LAG] == score.max()
    assert score[150 + moments.CHAT_LAG] < score.max()
    assert rate[150] == 80  # hype messages count double


def test_windows_never_overlap_and_stay_inside_the_vod():
    score = np.zeros(1000)
    score[[10, 50, 990]] = [3, 2, 1]
    wins = moments.windows(score, 3, 1000)
    assert all(0 <= a < b <= 1000 for a, b in wins)
    assert all(b1 <= a2 for (_, b1), (a2, _) in zip(wins, wins[1:], strict=False))
