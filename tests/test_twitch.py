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


def test_chat_failure_fails_the_vod_instead_of_skipping_chat(monkeypatch):
    def broken(*a):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(twitch, "fetch_chat", broken)
    with pytest.raises(ClipitError, match="retry later"):
        pipeline.get_chat({"extractor": "twitch:vod", "id": "v1", "duration": 60})
    assert pipeline.get_chat({"extractor": "youtube", "id": "x", "duration": 60}) == []
