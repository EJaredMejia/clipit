import datetime
import random

import pytest

from clipit import db, poll
from clipit.util import ClipitError

STREAMS = [
    ("big1", 30000, "EN"),
    ("es", 25000, "ES"),
    ("big2", 20000, "EN"),
    ("mid", 5000, "EN"),
    ("s1", 1500, "EN"),
    ("s2", 900, "EN"),
    ("s3", 1200, "FR"),
    ("tiny", 40, "EN"),
]


def test_candidates_big_first_then_small_in_range_and_language():
    got = poll.candidates(STREAMS, "EN", random.Random(1))
    assert got[:3] == [("top", "big1"), ("top", "big2"), ("top", "mid")]
    assert sorted(got[3:]) == [("small", "s1"), ("small", "s2")]
    assert ("top", "es") in poll.candidates(STREAMS, "")


def test_vod_age_counts_from_when_the_stream_ended():
    end = poll.ended({"publishedAt": "2026-10-20T18:00:00Z", "lengthSeconds": 32 * 3600})
    assert end == datetime.datetime(2026, 10, 22, 2, tzinfo=datetime.UTC)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Fake Twitch + pipeline. big2 has no VODs; VOD 3 always fails."""
    fresh = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=5)).isoformat()
    vods = {"big1": "1", "mid": "3", "s1": "4", "s2": "5"}
    monkeypatch.setattr(poll.twitch, "live_streams", lambda: STREAMS)
    monkeypatch.setattr(
        poll.twitch,
        "latest_vod",
        lambda login: login in vods and {"id": vods[login], "publishedAt": fresh, "lengthSeconds": 3600} or None,
    )
    calls = []

    def fake_process(url, out_root, **kw):
        vod_id = "v" + url.rsplit("/", 1)[1]
        calls.append(vod_id)
        work = out_root / vod_id
        work.mkdir()
        (work / "audio.m4a").write_text("x")
        if vod_id == "v3":
            raise ClipitError("sub-only VOD")
        return work, [
            {
                "idx": 1,
                "file": "clips/01_a.mp4",
                "start": 1.0,
                "end": 20.0,
                "title": "t",
                "hook": "h",
                "caption": "c",
                "youtube": "y",
                "streamer": "s",
                "trend": "",
                "sound": "",
                "score": 8.0,
                "why": "",
            }
        ]

    monkeypatch.setattr(poll, "process_vod", fake_process)
    conn = db.connect(tmp_path / "clipit.db")
    yield conn, calls, tmp_path
    conn.close()


def test_poll_pass(world):
    conn, calls, out = world
    poll.poll(conn, top_n=2, small_per_day=1, lang="EN", out_root=out)
    assert calls[:2] == ["v1", "v3"] and len(calls) == 3  # big2 has no VOD so mid takes its slot, then 1 small
    assert db.status(conn, "v1") == "done" and db.status(conn, "v3") == "failed"
    assert conn.execute("SELECT file FROM clips WHERE vod_id = 'v1'").fetchone()["file"] == "v1/clips/01_a.mp4"
    assert not list(out.glob("v*/audio.*"))  # audio deleted for done AND failed VODs

    poll.poll(conn, top_n=2, small_per_day=1, lang="EN", out_root=out)
    assert len(calls) == 3  # nothing new: v1 done, v3 waits RETRY_AFTER, today's small budget used


def test_one_streamers_vod_lookup_failing_does_not_end_the_pass(world, monkeypatch):
    conn, calls, out = world
    real = poll.twitch.latest_vod

    def flaky(login):
        if login == "big1":
            raise ClipitError("Twitch API: service timeout")
        return real(login)

    monkeypatch.setattr(poll.twitch, "latest_vod", flaky)
    poll.poll(conn, top_n=2, small_per_day=0, lang="EN", out_root=out)
    assert calls == ["v3"]  # big1 skipped, big2 has no VOD, mid still processed


def test_dry_run_touches_nothing(world):
    conn, calls, out = world
    poll.poll(conn, top_n=2, small_per_day=1, lang="EN", dry_run=True, out_root=out)
    assert calls == [] and conn.execute("SELECT count(*) FROM vods").fetchone()[0] == 0
