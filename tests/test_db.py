import datetime

import pytest

from clipit import db

T0 = datetime.datetime(2026, 10, 22, 12, tzinfo=datetime.UTC)
CLIP = {"idx": 1, "file": "v1/clips/01_a.mp4", "start": 1.0, "end": 20.0, "title": "t", "hook": "h", "caption": "c",
        "trend": "", "sound": "", "score": 8.0, "why": ""}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "clipit.db")
    yield c
    c.close()


def test_migrations_are_applied_once(tmp_path):
    db.connect(tmp_path / "x.db").close()
    c = db.connect(tmp_path / "x.db")  # reopening must not re-run migrations
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_claim_once_until_done(conn):
    assert db.claim(conn, "v1", "u", "s", "top", now=T0)
    assert not db.claim(conn, "v1", "u", "s", "top", now=T0)  # another run has it
    db.finish(conn, "v1", now=T0)
    assert not db.claim(conn, "v1", "u", "s", "top", now=T0 + datetime.timedelta(days=9))
    assert db.status(conn, "v1") == "done"


def test_crashed_claim_is_taken_over_after_it_goes_stale(conn):
    assert db.claim(conn, "v1", "u", "s", "top", now=T0)
    assert not db.claim(conn, "v1", "u", "s", "top", now=T0 + db.STALE_CLAIM / 2)
    assert db.claim(conn, "v1", "u", "s", "top", now=T0 + db.STALE_CLAIM * 2)


def test_failures_retry_after_a_wait_then_give_up(conn):
    t = T0
    for _ in range(db.MAX_ATTEMPTS):
        assert db.claim(conn, "v1", "u", "s", "top", now=t)
        db.finish(conn, "v1", error="boom", now=t)
        assert not db.claim(conn, "v1", "u", "s", "top", now=t + db.RETRY_AFTER / 2)  # too soon
        t += db.RETRY_AFTER * 2
    assert not db.claim(conn, "v1", "u", "s", "top", now=t)  # out of attempts
    row = conn.execute("SELECT attempts, error FROM vods").fetchone()
    assert row["attempts"] == db.MAX_ATTEMPTS and row["error"] == "boom"


def test_save_clips_upserts_and_keeps_ids_stable(conn):
    db.record_manual(conn, "v1", "u")
    db.save_clips(conn, "v1", [CLIP])
    first_id = conn.execute("SELECT id FROM clips").fetchone()[0]
    db.save_clips(conn, "v1", [{**CLIP, "title": "new"}])
    rows = conn.execute("SELECT id, title FROM clips").fetchall()
    assert [(r["id"], r["title"]) for r in rows] == [(first_id, "new")]


def test_clips_need_their_vod(conn):
    with pytest.raises(Exception, match="FOREIGN KEY"):
        db.save_clips(conn, "missing", [CLIP])


def test_small_done_since_counts_recent_successes_only(conn):
    for vid, err in (("v1", None), ("v2", "boom")):
        db.claim(conn, vid, "u", "s", "small", now=T0)
        db.finish(conn, vid, error=err, now=T0)
    assert db.small_done_since(conn, T0 - datetime.timedelta(hours=1)) == 1
    assert db.small_done_since(conn, T0 + datetime.timedelta(hours=1)) == 0
