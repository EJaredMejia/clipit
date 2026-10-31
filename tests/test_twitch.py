import io
import json
import urllib.error

import pytest

from clipit import pipeline, twitch
from clipit.util import ClipitError


def test_post_rides_out_network_errors(monkeypatch):
    monkeypatch.setattr(twitch, "RETRY_WAITS", (0, 0, 0))
    attempts = []

    def flaky(req, timeout):
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.URLError("[Errno 11001] getaddrinfo failed")  # the real failure seen in a live run
        return io.BytesIO(json.dumps({"data": {"ok": True}}).encode())

    monkeypatch.setattr(twitch.urllib.request, "urlopen", flaky)
    assert twitch.gql("query { ok }") == {"ok": True} and len(attempts) == 3


def test_post_gives_up_after_the_last_retry(monkeypatch):
    monkeypatch.setattr(twitch, "RETRY_WAITS", (0, 0))

    def down(req, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(twitch.urllib.request, "urlopen", down)
    with pytest.raises(urllib.error.URLError):
        twitch.gql("query { ok }")


def test_post_retries_twitchs_passing_service_errors(monkeypatch):
    monkeypatch.setattr(twitch, "RETRY_WAITS", (0, 0, 0))
    replies = [{"errors": [{"message": "service error"}]}, {"errors": [{"message": "service timeout"}]}, {"data": {"ok": 1}}]

    def flaky(req, timeout):
        return io.BytesIO(json.dumps(replies.pop(0)).encode())

    monkeypatch.setattr(twitch.urllib.request, "urlopen", flaky)
    assert twitch.gql("query { ok }") == {"ok": 1} and not replies


def test_lasting_gql_errors_fail_loudly(monkeypatch):
    monkeypatch.setattr(twitch, "RETRY_WAITS", (0,))
    attempts = []

    def reply(body):
        def urlopen(req, timeout):
            attempts.append(1)
            return io.BytesIO(json.dumps(body).encode())

        return urlopen

    monkeypatch.setattr(twitch.urllib.request, "urlopen", reply({"errors": [{"message": "service error"}]}))
    with pytest.raises(ClipitError, match="service error"):  # still down after every retry
        twitch.gql("query { ok }")
    assert len(attempts) == 2
    attempts.clear()
    monkeypatch.setattr(twitch.urllib.request, "urlopen", reply({"errors": [{"message": "Cannot query field"}]}))
    with pytest.raises(ClipitError, match="Cannot query field"):  # a query Twitch no longer accepts: no retry
        twitch.gql("query { ok }")
    assert len(attempts) == 1
    monkeypatch.setattr(twitch.urllib.request, "urlopen", reply([{"errors": [{"message": "PersistedQueryNotFound"}]}]))
    with pytest.raises(ClipitError, match="PersistedQueryNotFound"):
        twitch.gql_comments("1", 0)
    monkeypatch.setattr(twitch.urllib.request, "urlopen", reply([{"data": {"video": None}}]))
    with pytest.raises(ClipitError, match="unexpected reply"):  # a deleted VOD, or a changed reply shape
        twitch.gql_comments("1", 0)


def test_chat_failure_fails_the_vod_instead_of_skipping_chat(monkeypatch):
    def broken(*a):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(twitch, "fetch_chat", broken)
    with pytest.raises(ClipitError, match="retry later"):
        pipeline.get_chat({"extractor": "twitch:vod", "id": "v1", "duration": 60})
    assert pipeline.get_chat({"extractor": "youtube", "id": "x", "duration": 60}) == []


def test_fetch_chat_stops_at_the_vods_end(monkeypatch):
    def comments(vid, t):  # like Twitch: an offset past the end is an error, and the last page still says "more"
        if t > 100:
            raise ClipitError("Twitch chat API: service error")
        node = {"id": str(t), "contentOffsetSeconds": t + 2, "message": {"fragments": [{"text": "hi"}]}}
        return {"edges": [{"node": node}], "pageInfo": {"hasNextPage": True}}

    monkeypatch.setattr(twitch, "gql_comments", comments)
    got = twitch.fetch_chat("1", 99.5, slice_len=300)
    assert got and all(t <= 101.5 for t, _ in got)


def test_top_clipped_adds_up_a_streamers_clips_across_games(monkeypatch):
    def clip(login, views):
        return {"node": {"viewCount": views, "language": "EN", "broadcaster": login and {"login": login}}}

    games = [{"node": {"clips": {"edges": [clip("a", 5), clip("b", 7), clip(None, 99)]}}}, {"node": {"clips": None}}]
    games.append({"node": {"clips": {"edges": [clip("a", 4)]}}})
    monkeypatch.setattr(twitch, "gql", lambda query: {"games": {"edges": games}})
    assert twitch.top_clipped() == [("a", 9, "EN"), ("b", 7, "EN")]
