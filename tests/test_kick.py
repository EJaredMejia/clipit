import datetime

import numpy as np
import pytest

from clipit import kick, moments, pipeline
from clipit.util import ClipitError

START = datetime.datetime(2026, 10, 24, 20, tzinfo=datetime.UTC).timestamp()


def at(t: float) -> str:
    return datetime.datetime.fromtimestamp(START + t, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_fetch_chat_pages_back_through_each_slice(monkeypatch):
    """A fake chat with one message every 10 s; pages hold 5 messages before the cursor, newest first, like Kick's."""
    chat = [{"id": str(t), "created_at": at(t), "content": f"m{t} [emote:39:KEKW]"} for t in range(0, 600, 10)]

    def history(channel_id, before):
        page = [m for m in reversed(chat) if START + int(m["id"]) < before / 1e6][:5]
        return {"messages": page, "cursor": str(int((START + int(page[-1]["id"])) * 1e6)) if page else ""}

    monkeypatch.setattr(kick, "history", history)
    got = kick.fetch_chat(668, START, 600, slice_len=100)
    assert sorted(m[0] for m in got) == list(range(0, 600, 10))  # every message once, none lost at slice edges
    assert [m[1] for m in got if m[0] == 30] == ["m30 KEKW"]


def busy_chat(monkeypatch, seconds: int) -> list:
    """10 messages a second, 5 to a page: a page covers 0.5 s, so reading every page would take 2 per second of VOD.
    Returns the list of `before` cursors requested."""
    chat = [{"id": str(i), "created_at": at(i / 10), "content": "hello"} for i in range(seconds * 10)]
    calls = []

    def history(channel_id, before):
        calls.append(before)
        page = [m for m in reversed(chat) if START + int(m["id"]) / 10 < before / 1e6][:5]
        return {"messages": page, "cursor": str(int((START + int(page[-1]["id"]) / 10) * 1e6)) if page else ""}

    monkeypatch.setattr(kick, "history", history)
    return calls


def test_busy_chat_is_sampled_with_weights_that_keep_the_rate(monkeypatch):
    calls = busy_chat(monkeypatch, 600)
    monkeypatch.setattr(kick, "MAX_PAGES", 60)  # 600 s / 60 pages: one page every 10 s
    got = kick.fetch_chat(668, START, 600, slice_len=100)
    assert len(calls) <= 66  # MAX_PAGES, plus one per slice for the page that crosses its start
    rate = sum(m[2] for m in got) / 600
    assert 8 < rate < 12  # still ~10 messages a second, as weights


def test_steady_sampled_chat_scores_flat(monkeypatch):
    """Samples 20 s apart, wider than the 15 s smoothing: steady chat must not turn into a row of peaks."""
    busy_chat(monkeypatch, 600)
    monkeypatch.setattr(kick, "MAX_PAGES", 30)
    _, _, c = moments.signals(600, np.zeros(600), kick.fetch_chat(668, START, 600, slice_len=100))
    smoothed = moments.smooth(c, 15)[20:-20]  # away from the VOD's edges
    assert smoothed.max() < 1.5 * smoothed.min()  # some ripple: 5 messages a page here, ~25 on Kick


def test_chat_stops_at_the_end_of_the_vod(monkeypatch):
    calls = busy_chat(monkeypatch, 900)  # chat goes on after the stream ended (the channel's history is channel-wide)
    got = kick.fetch_chat(668, START, 550, slice_len=300)  # the second slice ends early, at 550 s
    assert max(calls) <= (START + 550) * 1e6 and max(m[0] for m in got) < 550


def test_live_streams_pages_and_drops_odd_slugs(monkeypatch):
    pages = {
        "": {
            "livestreams": [{"channel": {"slug": "a"}, "viewer_count": 9, "language": "en"}],
            "pagination": {"next_cursor": "X"},
        },
        "&after=X": {
            "livestreams": [
                {"channel": {"slug": "b-c"}, "viewer_count": 99, "language": "en"},
                {"channel": {"slug": "../x"}, "viewer_count": 5},
            ],
            "pagination": {"next_cursor": ""},
        },
    }
    monkeypatch.setattr(kick, "get", lambda url: {"data": pages[url.partition("viewer_count_desc")[2]]})
    assert kick.live_streams() == [("b-c", 99, "EN"), ("a", 9, "EN")]


def test_latest_vod_skips_live_and_sub_only(monkeypatch):
    vods = [
        {"id": "live", "is_live": True, "status": "public", "duration": 0, "start_time": "x"},
        {"id": "sub", "is_live": False, "status": "subscriber_only", "duration": 9, "start_time": "x"},
        {
            "id": "ok",
            "is_live": False,
            "status": "public",
            "duration": 3600,
            "start_time": "2026-10-24T20:50:47Z",
            "language": "en",
        },
    ]
    monkeypatch.setattr(kick, "get", lambda url: {"id": 668} if "/v2/" in url else {"data": vods})
    vod = kick.latest_vod("xqc")
    assert vod == {"id": "ok", "publishedAt": "2026-10-24T20:50:47Z", "lengthSeconds": 3600, "language": "EN"}
    assert vod and kick.vod_ref("xqc", vod) == ("ok", "https://kick.com/xqc/videos/ok")
    with pytest.raises(ClipitError):
        kick.latest_vod("../../evil")


def test_kick_chat_failure_fails_the_vod(monkeypatch):
    def broken(*a):
        raise OSError("down")

    monkeypatch.setattr(kick, "fetch_chat", broken)
    with pytest.raises(ClipitError, match="retry later"):
        pipeline.get_chat({"extractor": "kick:vod", "id": "x", "channel_id": "668", "timestamp": START, "duration": 60})


def test_top_clipped_adds_up_views_and_reads_languages_off_vods(monkeypatch):
    pages = {
        "https://kick.com/api/v2/clips?sort=view&time=day": {
            "clips": [
                {"channel": {"slug": "a"}, "views": 5},
                {"channel": {"slug": "b"}, "views": 7},
                {"channel": {"slug": "../evil"}, "views": 99},
            ],
            "nextCursor": "c 1",
        },
        "https://kick.com/api/v2/clips?sort=view&time=day&cursor=c%201": {
            "clips": [
                {"channel": {"slug": "a"}, "views": 4},
                {"channel": {"slug": "banned"}, "views": 3},
                {"channel": {"slug": "novods"}, "views": 2},
                {"channel": {"slug": "tiny"}, "views": 1},
            ]
        },
    }

    def vod(slug):
        if slug == "banned":
            raise OSError("404")
        return {"a": {"language": "EN"}, "b": {"language": "ES"}, "tiny": {"language": "EN"}}.get(slug)

    monkeypatch.setattr(kick, "MIN_CLIP_VIEWS", 2)
    monkeypatch.setattr(kick, "get", pages.__getitem__)
    monkeypatch.setattr(kick, "latest_vod", vod)
    assert kick.top_clipped() == [("a", 9, "EN"), ("b", 7, "ES")]
