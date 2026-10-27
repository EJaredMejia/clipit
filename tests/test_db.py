import datetime
import time

import pytest

from clipit import db

T0 = datetime.datetime(2026, 10, 22, 12, tzinfo=datetime.UTC)
CLIP = {
    "idx": 1,
    "file": "v1/clips/01_a.mp4",
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


def test_a_deleted_clips_id_is_never_reused(conn):
    db.record_manual(conn, "v1", "u")
    db.save_clips(conn, "v1", [CLIP])
    (old_id,) = conn.execute("SELECT id FROM clips").fetchone()
    db.save_clips(conn, "v1", [{**CLIP, "start": 60.0, "end": 80.0}])  # replaces the only (so highest-id) clip
    assert conn.execute("SELECT id FROM clips").fetchone()[0] != old_id


def test_migration_keeps_clips_saved_under_the_old_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:1])
    old = db.connect(tmp_path / "x.db")
    old.execute("INSERT INTO vods (id, url, tier, status) VALUES ('v1', 'u', 'manual', 'done')")  # written by the old code
    old.execute("""INSERT INTO clips (vod_id, idx, file, start_s, end_s, title, hook, caption, trend, sound, score, why,
                   created_at) VALUES ('v1', 1, 'f', 1, 20, 't', 'h', 'c', '', '', 8, '', 'now')""")
    old.close()
    monkeypatch.undo()
    c = db.connect(tmp_path / "x.db")
    assert [tuple(r) for r in c.execute("SELECT id, idx, start_s FROM clips")] == [(1, 1, 1.0)]
    db.save_clips(c, "v1", [{**CLIP, "start": 1.0, "end": 20.0, "title": "new"}])  # upserts on the new key
    assert [tuple(r) for r in c.execute("SELECT id, title FROM clips")] == [(1, "new")]
    db.save_clips(c, "v1", [{**CLIP, "start": 60.0, "end": 80.0}])  # ids copied in by the migration aren't reused
    assert c.execute("SELECT id FROM clips").fetchone()[0] != 1


def test_resaving_keeps_the_users_verdict_and_clips_they_kept_or_posted(conn):
    db.record_manual(conn, "v1", "u")
    a, b, c = CLIP, {**CLIP, "start": 30.0, "end": 50.0}, {**CLIP, "start": 60.0, "end": 80.0}
    db.save_clips(conn, "v1", [a, b, c])
    ids = dict(conn.execute("SELECT start_s, id FROM clips").fetchall())
    assert db.set_verdict(conn, ids[1.0], "keep") and db.set_upload(conn, ids[30.0], "tiktok", "")
    db.save_clips(conn, "v1", [{**a, "title": "re-rendered"}])  # a re-pick that drops b and c
    rows = {r["start_s"]: r for r in conn.execute("SELECT start_s, title, verdict FROM clips")}
    assert set(rows) == {1.0, 30.0}  # b was posted: kept; c was neither kept nor posted: gone
    assert rows[1.0]["title"] == "re-rendered" and rows[1.0]["verdict"] == "keep"


def test_verdicts_and_uploads(conn):
    db.record_manual(conn, "v1", "https://www.twitch.tv/videos/1")
    db.save_clips(conn, "v1", [{**CLIP, "streamer": ""}])
    (cid,) = conn.execute("SELECT id FROM clips").fetchone()
    assert not db.set_verdict(conn, cid + 1, "keep") and not db.set_upload(conn, cid + 1, "tiktok", "")
    assert db.set_upload(conn, cid, "youtube", "https://youtu.be/x")
    assert db.set_upload(conn, cid, "youtube", "https://youtu.be/y")  # marking it again updates the link
    (clip,) = db.overview(conn)["clips"]
    assert clip["uploads"]["youtube"]["url"] == "https://youtu.be/y" and clip["verdict"] is None
    assert db.set_upload(conn, cid, "youtube", None) and db.overview(conn)["clips"][0]["uploads"] == {}
    with pytest.raises(Exception, match="CHECK"):
        db.set_verdict(conn, cid, "maybe")


def test_overview_falls_back_to_the_vods_streamer(conn):
    db.claim(conn, "v1", "u", "zackrawrr", "top", now=T0)
    db.save_clips(conn, "v1", [{**CLIP, "streamer": ""}])  # clips saved before the streamer column existed
    db.record_manual(conn, "v2", "u2")
    db.save_clips(conn, "v2", [{**CLIP, "streamer": "caseoh_"}])
    data = db.overview(conn)
    assert {c["vod_id"]: c["streamer"] for c in data["clips"]} == {"v1": "zackrawrr", "v2": "caseoh_"}
    assert {v["id"]: v["streamer"] for v in data["vods"]} == {"v1": "zackrawrr", "v2": "caseoh_"}


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


def kept_clips(conn, n: int) -> list[int]:
    """n clips the user kept from a polled VOD, oldest first."""
    db.claim(conn, "v1", "u", "s", "top", now=T0)
    db.finish(conn, "v1", now=T0)
    db.save_clips(conn, "v1", [{**CLIP, "idx": i, "start": 100.0 * i, "end": 100.0 * i + 20} for i in range(n)])
    ids = [r[0] for r in conn.execute("SELECT id FROM clips ORDER BY idx")]
    for i, cid in enumerate(ids):
        db.set_verdict(conn, cid, "keep")
        conn.execute("UPDATE clips SET created_at = ? WHERE id = ?", ((T0 + datetime.timedelta(minutes=i)).isoformat(), cid))
    return ids


def test_claim_upload_posts_the_newest_kept_clips_at_the_paced_rate(conn):
    a, b, skipped, unreviewed, c = kept_clips(conn, 5)
    db.set_verdict(conn, skipped, "skip")
    db.set_verdict(conn, unreviewed, None)
    conn.execute("UPDATE clips SET score = ? WHERE id = ?", (db.AUTO_SCORE - 1, unreviewed))  # below the AI's bar
    h = datetime.timedelta(hours=1)

    def claim(at):
        row = db.claim_upload(conn, "youtube", 2, 4 * h, now=T0 + at)
        return row and row["id"]

    assert claim(0 * h) == c
    assert claim(1 * h) is None  # too soon after the last post, even while it's still uploading
    assert claim(4 * h) == b
    assert claim(8 * h) is None  # 2 posts in the last 24 hours already
    assert claim(25 * h) == a
    assert claim(50 * h) is None  # everything kept is posted
    assert db.claim_upload(conn, "tiktok", 2, 4 * h, now=T0) is not None  # each platform has its own queue


def test_claim_upload_skips_stale_and_hand_run_clips(conn):
    (old,) = kept_clips(conn, 1)
    assert db.claim_upload(conn, "youtube", 3, datetime.timedelta(hours=1), now=T0 + db.FRESH * 2) is None  # trend passed
    db.record_manual(conn, "v2", "u")
    db.save_clips(conn, "v2", [CLIP])
    (manual,) = [r[0] for r in conn.execute("SELECT id FROM clips WHERE vod_id = 'v2'")]
    db.set_verdict(conn, manual, "keep")
    conn.execute("UPDATE clips SET created_at = ? WHERE id = ?", (T0.isoformat(), manual))
    row = db.claim_upload(conn, "youtube", 3, datetime.timedelta(hours=1), now=T0)
    assert row and row["id"] == old  # not the newer hand-run one


def test_claim_upload_takes_kept_first_then_high_scoring_unreviewed(conn):
    low, high, kept, skipped = kept_clips(conn, 4)
    for cid, verdict, score in ((low, None, db.AUTO_SCORE - 1), (high, None, db.AUTO_SCORE), (skipped, "skip", 10)):
        db.set_verdict(conn, cid, verdict)
        conn.execute("UPDATE clips SET score = ? WHERE id = ?", (score, cid))
    claimed = [db.claim_upload(conn, "youtube", 9, datetime.timedelta(0), now=T0) for _ in range(3)]
    assert [r and r["id"] for r in claimed] == [kept, high, None]  # the skipped clip is newest and scores 10


def test_failed_uploads_retry_after_a_wait_then_give_up(conn):
    (cid,) = kept_clips(conn, 1)
    t = T0
    for _ in range(db.MAX_ATTEMPTS):
        assert db.claim_upload(conn, "youtube", 3, datetime.timedelta(hours=4), now=t)
        db.fail_upload(conn, cid, "youtube", "boom")
        assert db.overview(conn)["clips"][0]["uploads"] == {}  # a failed upload isn't shown as posted
        assert not db.claim_upload(conn, "youtube", 3, datetime.timedelta(hours=4), now=t + db.RETRY_AFTER / 2)
        t += db.RETRY_AFTER * 2
    assert not db.claim_upload(conn, "youtube", 3, datetime.timedelta(hours=4), now=t)  # out of attempts
    assert db.set_upload(conn, cid, "youtube", "https://youtu.be/x")  # the user posted it by hand
    assert db.overview(conn)["clips"][0]["uploads"]["youtube"]["url"] == "https://youtu.be/x"


def test_an_upload_that_never_finished_is_reported(conn):
    (cid,) = kept_clips(conn, 1)
    assert db.claim_upload(conn, "youtube", 3, datetime.timedelta(hours=4), now=T0)
    assert db.stuck_uploads(conn, "youtube", T0 - datetime.timedelta(hours=1)) == []  # still running
    assert db.stuck_uploads(conn, "youtube", T0 + datetime.timedelta(hours=1)) == [cid]
    db.set_upload(conn, cid, "youtube", "https://youtu.be/x")
    assert db.stuck_uploads(conn, "youtube", T0 + datetime.timedelta(hours=1)) == []


def test_the_users_edits_survive_a_rerender(conn):
    db.record_manual(conn, "v1", "u")
    db.save_clips(conn, "v1", [CLIP])
    (cid,) = conn.execute("SELECT id FROM clips").fetchone()
    assert db.set_text(conn, cid, "title", "my title") and not db.set_text(conn, cid + 1, "title", "x")
    db.save_clips(conn, "v1", [{**CLIP, "title": "AI title", "youtube": "AI description", "score": 9.0}])
    row = conn.execute("SELECT title, youtube, score FROM clips").fetchone()
    assert tuple(row) == ("my title", "y", 9.0)  # all post text is the user's now; the rest still updates
    with pytest.raises(ValueError):
        db.set_text(conn, cid, "file", "../x")


def test_editing_an_old_clip_keeps_the_caption_it_showed_as_description(conn):
    db.record_manual(conn, "v1", "u")
    db.save_clips(conn, "v1", [{**CLIP, "youtube": ""}])  # made before YouTube descriptions existed
    (cid,) = conn.execute("SELECT id FROM clips").fetchone()
    db.set_text(conn, cid, "title", "new")
    assert tuple(conn.execute("SELECT title, youtube FROM clips").fetchone()) == ("new", "c")


def test_a_description_the_user_cleared_stays_empty(conn):
    db.record_manual(conn, "v1", "u")
    db.save_clips(conn, "v1", [CLIP])
    (cid,) = conn.execute("SELECT id FROM clips").fetchone()
    db.set_text(conn, cid, "youtube", "")  # the user's choice: post it with no description
    db.set_text(conn, cid, "title", "later edit")
    assert tuple(conn.execute("SELECT title, youtube FROM clips").fetchone()) == ("later edit", "")
