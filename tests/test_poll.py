import datetime
from types import SimpleNamespace

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


def test_candidates_are_big_streamers_in_language_only():
    streams = [(*s, poll.twitch) for s in STREAMS]
    assert [s[0] for s in poll.candidates(streams, "EN")] == ["big1", "big2", "mid"]
    assert "es" in [s[0] for s in poll.candidates(streams, "")]


def test_candidates_alternate_sites():
    tw = [("t1", 90000, "EN", poll.twitch), ("t2", 80000, "EN", poll.twitch), ("t3", 70000, "EN", poll.twitch)]
    ki = [("k1", 20000, "EN", poll.kick), ("k2", 3000, "EN", poll.kick), ("ks1", 500, "EN", poll.kick)]
    got = [s[0] for s in poll.candidates(tw + ki, "EN")]
    assert got == ["k1", "t1", "k2", "t2", "t3"]


def test_blocked_streamers_are_never_candidates():
    streams = [("Pokimane", 30000, "EN", poll.twitch), ("big1", 20000, "EN", poll.twitch)]
    got = [s[0] for s in poll.candidates(streams, "EN", first=[("kick", "POKIMANE"), ("twitch", "pokimane")])]
    assert got == ["big1"]


def test_most_clipped_come_first_on_both_sites_live_or_not_and_only_once():
    streams = [("big1", 30000, "EN", poll.twitch), ("Clavicular", 9000, "EN", poll.kick), ("s1", 1500, "EN", poll.twitch)]
    clipped = [
        ("t1", 90000, "EN", poll.twitch),  # offline is fine: we clip the latest finished VOD
        ("t2", 80000, "EN", poll.twitch),
        ("clavicular", 5000, "EN", poll.kick),
        ("spanish", 70000, "ES", poll.kick),
        ("riotgames", 60000, "EN", poll.twitch),
    ]
    got = [(s[0], s[3]) for s in poll.candidates(streams, "EN", clipped)]
    assert got == [("clavicular", poll.kick), ("t1", poll.twitch), ("t2", poll.twitch), ("big1", poll.twitch)]


def test_prioritized_streamers_are_clipped_first(world, monkeypatch):
    conn, calls, out = world
    monkeypatch.setattr(poll.twitch, "top_clipped", lambda: [("big1", 50000, "EN")])
    poll.poll(conn, top_n=1, lang="EN", out_root=out, prioritize=[("twitch", "mid")])
    assert calls == ["v3", "v1"]  # mid goes ahead of the most clipped big1, who gets tried as mid's VOD fails


def test_live_streams_keep_both_sites_and_survive_one_being_down(monkeypatch):
    monkeypatch.setattr(poll.kick, "live_streams", lambda: [("xqc", 90000, "EN"), ("kickonly", 1000, "EN")])
    monkeypatch.setattr(poll.twitch, "live_streams", lambda: [("xQc", 50000, "EN"), ("big1", 30000, "EN")])
    got = [(s[0], s[3]) for s in poll.live_streams()]
    assert got == [("xqc", poll.kick), ("xQc", poll.twitch), ("big1", poll.twitch), ("kickonly", poll.kick)]

    def down():
        raise ClipitError("Kick API: 503")

    monkeypatch.setattr(poll.kick, "live_streams", down)
    assert [s[0] for s in poll.live_streams()] == ["xQc", "big1"]  # Kick down: Twitch still clipped
    monkeypatch.setattr(poll.twitch, "live_streams", down)
    with pytest.raises(ClipitError, match="any site"):
        poll.live_streams()


def test_vod_age_counts_from_when_the_stream_ended():
    end = poll.ended({"publishedAt": "2026-10-20T18:00:00Z", "lengthSeconds": 32 * 3600})
    assert end == datetime.datetime(2026, 10, 22, 2, tzinfo=datetime.UTC)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Fake Twitch + pipeline. big2 has no VODs; VOD 3 always fails."""
    fresh = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=5)).isoformat()
    vods = {"big1": "1", "mid": "3", "s1": "4", "s2": "5"}
    monkeypatch.setattr(poll.twitch, "live_streams", lambda: STREAMS)
    monkeypatch.setattr(poll.kick, "live_streams", lambda: [])
    monkeypatch.setattr(poll.twitch, "top_clipped", lambda: [])
    monkeypatch.setattr(poll.kick, "top_clipped", lambda: [])
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
    assert poll.poll(conn, top_n=3, lang="EN", out_root=out) == 0  # v1 worked: a broken VOD among good ones is no failed pass
    assert calls == ["v1", "v3"]  # big2 has no VOD so mid takes its slot; s1 and s2 are too small
    assert db.status(conn, "v1") == "done" and db.status(conn, "v3") == "failed"
    assert conn.execute("SELECT file FROM clips WHERE vod_id = 'v1'").fetchone()["file"] == "v1/clips/01_a.mp4"
    assert not list(out.glob("v*/audio.*"))  # audio deleted for done AND failed VODs

    assert poll.poll(conn, top_n=3, lang="EN", out_root=out) == 0
    assert len(calls) == 2  # nothing new: v1 done, v3 waits RETRY_AFTER


def test_a_repeat_pass_gives_an_already_clipped_streamers_slot_to_the_next(world):
    conn, calls, out = world
    poll.poll(conn, top_n=1, lang="EN", out_root=out)
    poll.poll(conn, top_n=1, lang="EN", out_root=out)
    assert calls == ["v1", "v3"]  # big1 is done, big2 has no VOD, mid gets the slot


def test_one_streamers_vod_lookup_failing_does_not_end_the_pass(world, monkeypatch):
    conn, calls, out = world
    real = poll.twitch.latest_vod

    def flaky(login):
        if login == "big1":
            raise ClipitError("Twitch API: service timeout")
        return real(login)

    monkeypatch.setattr(poll.twitch, "latest_vod", flaky)
    poll.poll(conn, top_n=2, lang="EN", out_root=out)
    assert calls == ["v3"]  # big1 skipped, big2 has no VOD, mid still processed


def test_most_clipped_still_clipped_when_both_live_lists_are_down(world, monkeypatch):
    conn, calls, tmp_path = world

    def down():
        raise ClipitError("API down")

    monkeypatch.setattr(poll.twitch, "live_streams", down)
    monkeypatch.setattr(poll.kick, "live_streams", down)
    with pytest.raises(ClipitError, match="any site"):  # no clip ranking either: the pass fails loudly
        poll.poll(conn, out_root=tmp_path)
    monkeypatch.setattr(poll.twitch, "top_clipped", lambda: [("big1", 50000, "EN")])
    poll.poll(conn, out_root=tmp_path)
    assert calls == ["v1"]


def test_a_failing_clip_ranking_falls_back_to_live_viewers(world, monkeypatch):
    conn, calls, out = world

    def down():
        raise ClipitError("Twitch API: clips went away")

    monkeypatch.setattr(poll.twitch, "top_clipped", down)
    monkeypatch.setattr(poll.kick, "top_clipped", down)
    poll.poll(conn, top_n=1, lang="EN", out_root=out)
    assert calls == ["v1"]  # big1, the biggest live


def test_dry_run_touches_nothing(world):
    conn, calls, out = world
    poll.poll(conn, top_n=2, lang="EN", dry_run=True, out_root=out)
    assert calls == [] and conn.execute("SELECT count(*) FROM vods").fetchone()[0] == 0


def weak_unless(monkeypatch, good_url_end):
    real = poll.process_vod

    def scored(url, out_root, **kw):
        work, clips = real(url, out_root, **kw)
        return work, [{**c, "score": 8.0 if url.endswith(good_url_end) else 5.0} for c in clips]

    monkeypatch.setattr(poll, "process_vod", scored)


def test_a_pass_goes_on_past_top_n_until_it_has_a_good_clip_and_then_stops(world, monkeypatch):
    conn, calls, out = world
    monkeypatch.setattr(poll.twitch, "live_streams", lambda: [("big1", 30000, "EN"), ("s1", 3000, "EN"), ("s2", 3000, "EN")])
    weak_unless(monkeypatch, "/4")
    poll.poll(conn, top_n=1, out_root=out)
    assert calls == ["v1", "v4"]  # s2 comes next, but the pass has its good clip


def test_a_pass_with_no_good_clip_gives_up_after_extra_vods(world, monkeypatch):
    conn, calls, out = world
    weak_unless(monkeypatch, "/never")
    monkeypatch.setattr(poll, "EXTRA", 1)
    poll.poll(conn, top_n=1, out_root=out)
    assert len(calls) == 2


def test_everyone_the_search_finds_is_clipped_whatever_top_n_says(world, monkeypatch):
    conn, calls, out = world
    found = [("twitch", "s2", "banned"), ("twitch", "s1", "a fight"), ("twitch", "pgl", "a final"), ("kick", "nobody", "x")]
    monkeypatch.setattr(poll.ai, "find_hot", lambda cmd: found)
    monkeypatch.setattr(poll.kick, "latest_vod", lambda login: None)
    poll.poll(conn, top_n=1, out_root=out, search_ai="search")
    assert calls == ["v5", "v4"]  # both found, with a slot for one; big1 waits as the pass has its good clip


def test_a_failed_search_still_clips_by_the_ranking(world, monkeypatch):
    conn, calls, out = world

    def broken(cmd):
        raise ClipitError("no web")

    monkeypatch.setattr(poll.ai, "find_hot", broken)
    poll.poll(conn, top_n=1, out_root=out, search_ai="search")
    assert calls == ["v1"]


def test_a_failed_clip_lookup_only_loses_the_hint():
    def broken(login, vod):
        raise OSError("down")

    assert poll.moments_of(SimpleNamespace(clip_moments=broken), "a", {}) == []


def test_a_pass_where_every_vod_failed_says_so(world):
    conn, calls, out = world
    poll.poll(conn, top_n=1, lang="EN", out_root=out)  # v1 works
    assert poll.poll(conn, top_n=1, lang="EN", out_root=out) == 1  # only v3 left, and it fails
    assert calls == ["v1", "v3"]
