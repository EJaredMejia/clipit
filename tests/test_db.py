import datetime
import time

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


def test_clips_are_identified_by_their_moment_not_their_rank(conn):
    db.record_manual(conn, "v1", "u")
    a, b = CLIP, {**CLIP, "idx": 2, "file": "v1/clips/02_b.mp4", "start": 30.0, "end": 50.0}
    db.save_clips(conn, "v1", [a, b])
    ids = dict(conn.execute("SELECT start_s, id FROM clips").fetchall())
    c = {**CLIP, "start": 60.0, "end": 80.0}  # a re-pick: b moves to rank 1, a is dropped, a new moment c is rank 2
    db.save_clips(conn, "v1", [{**b, "idx": 1}, {**c, "idx": 2}])
    rows = conn.execute("SELECT id, idx, start_s FROM clips ORDER BY idx").fetchall()
    assert [(r["idx"], r["start_s"]) for r in rows] == [(1, 30.0), (2, 60.0)]
    assert rows[0]["id"] == ids[30.0] and rows[1]["id"] not in ids.values()


def test_migration_keeps_clips_saved_under_the_old_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:1])
    old = db.connect(tmp_path / "x.db")
    db.record_manual(old, "v1", "u")
    old.execute("""INSERT INTO clips (vod_id, idx, file, start_s, end_s, title, hook, caption, trend, sound, score, why,
                   created_at) VALUES ('v1', 1, 'f', 1, 20, 't', 'h', 'c', '', '', 8, '', 'now')""")
    old.close()
    monkeypatch.undo()
    c = db.connect(tmp_path / "x.db")
    assert [tuple(r) for r in c.execute("SELECT id, idx, start_s FROM clips")] == [(1, 1, 1.0)]
    db.save_clips(c, "v1", [{**CLIP, "start": 1.0, "end": 20.0, "title": "new"}])  # upserts on the new key
    assert [tuple(r) for r in c.execute("SELECT id, title FROM clips")] == [(1, "new")]


def test_heartbeat_keeps_a_long_run_claimed(conn, monkeypatch):
    monkeypatch.setattr(db, "HEARTBEAT", datetime.timedelta(seconds=0.01))
    assert db.claim(conn, "v1", "u", "s", "top", now=T0)
    with db.heartbeat(conn, "v1"):
        deadline = time.monotonic() + 5
        while conn.execute("SELECT claimed_at FROM vods").fetchone()[0] == T0.isoformat() and time.monotonic() < deadline:
            time.sleep(0.01)
    refreshed = datetime.datetime.fromisoformat(conn.execute("SELECT claimed_at FROM vods").fetchone()[0])
    assert refreshed > T0
    assert not db.claim(conn, "v1", "u", "s", "top", now=refreshed + db.STALE_CLAIM / 2)  # still alive, not stale


def test_clips_need_their_vod(conn):
    with pytest.raises(Exception, match="FOREIGN KEY"):
        db.save_clips(conn, "missing", [CLIP])


def test_small_done_since_counts_recent_successes_only(conn):
    for vid, err in (("v1", None), ("v2", "boom")):
        db.claim(conn, vid, "u", "s", "small", now=T0)
        db.finish(conn, vid, error=err, now=T0)
    assert db.small_done_since(conn, T0 - datetime.timedelta(hours=1)) == 1
    assert db.small_done_since(conn, T0 + datetime.timedelta(hours=1)) == 0
