"""SQLite catalog: which VODs were processed (the polling job queue) and which clips exist (what uploaders read).

Per-VOD working files (audio, transcripts, renders) stay on disk in out/<vod-id>/ as a cache.
Everything shared across runs lives here, never in ad-hoc JSON files.
"""

import contextlib
import datetime
import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

from clipit.util import logger, utcnow

# One entry per schema version, applied in order and recorded in PRAGMA user_version.
# Never edit an entry that has shipped: append a new one (ALTER TABLE ...) instead.
MIGRATIONS = [
    """
    CREATE TABLE vods (
        id          TEXT PRIMARY KEY,          -- yt-dlp id, also the out/<id>/ folder name (Twitch: 'v' + digits)
        url         TEXT NOT NULL,
        login       TEXT,                      -- streamer, when known
        tier        TEXT NOT NULL CHECK (tier IN ('top', 'small', 'manual')),
        status      TEXT NOT NULL CHECK (status IN ('processing', 'done', 'failed')),
        attempts    INTEGER NOT NULL DEFAULT 0,
        error       TEXT,
        claimed_at  TEXT,                      -- ISO 8601 UTC
        finished_at TEXT
    );
    CREATE INDEX vods_tier_finished ON vods (tier, status, finished_at);
    CREATE TABLE clips (
        id         INTEGER PRIMARY KEY,
        vod_id     TEXT NOT NULL REFERENCES vods (id) ON DELETE CASCADE,
        idx        INTEGER NOT NULL,           -- 1-based order the AI ranked it
        file       TEXT NOT NULL,              -- relative to the out folder
        start_s    REAL NOT NULL,
        end_s      REAL NOT NULL,
        title      TEXT NOT NULL,
        hook       TEXT NOT NULL,
        caption    TEXT NOT NULL,
        trend      TEXT NOT NULL,
        sound      TEXT NOT NULL,
        score      REAL NOT NULL,
        why        TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (vod_id, idx)
    );
    """,
    # Clips are identified by their moment, not their rank: a re-pick that moves a moment keeps its id (uploads
    # point at it), and a new moment at an old rank gets a new id. SQLite can't drop a constraint, so rebuild.
    # AUTOINCREMENT: a deleted clip's id is never handed to a new one (plain INTEGER PRIMARY KEY reuses the highest).
    """
    CREATE TABLE clips_new (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        vod_id     TEXT NOT NULL REFERENCES vods (id) ON DELETE CASCADE,
        idx        INTEGER NOT NULL,           -- 1-based order the AI ranked it in the latest pick
        file       TEXT NOT NULL,              -- relative to the out folder
        start_s    REAL NOT NULL,
        end_s      REAL NOT NULL,
        title      TEXT NOT NULL,
        hook       TEXT NOT NULL,
        caption    TEXT NOT NULL,
        trend      TEXT NOT NULL,
        sound      TEXT NOT NULL,
        score      REAL NOT NULL,
        why        TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (vod_id, start_s, end_s)
    );
    INSERT OR IGNORE INTO clips_new SELECT * FROM clips ORDER BY id;
    DROP TABLE clips;
    ALTER TABLE clips_new RENAME TO clips;
    """,
    # Post text built in code (caption is TikTok's), the user's review, and where each clip was posted.
    """
    ALTER TABLE clips ADD COLUMN streamer TEXT NOT NULL DEFAULT '';  -- Twitch login credited in the post text
    ALTER TABLE clips ADD COLUMN youtube TEXT NOT NULL DEFAULT '';   -- YouTube description, ready to paste
    ALTER TABLE clips ADD COLUMN verdict TEXT CHECK (verdict IN ('keep', 'skip'));  -- the user's call, NULL until made
    CREATE TABLE uploads (
        clip_id     INTEGER NOT NULL REFERENCES clips (id) ON DELETE CASCADE,
        platform    TEXT NOT NULL CHECK (platform IN ('tiktok', 'youtube')),
        url         TEXT NOT NULL DEFAULT '',  -- link to the post, '' when not given
        uploaded_at TEXT NOT NULL,
        PRIMARY KEY (clip_id, platform)
    );
    """,
    # Automatic uploads: a row is claimed as 'uploading' before the video is sent, so a clip is never posted twice.
    # Rows marked by hand in the dashboard are 'done'.
    """
    ALTER TABLE uploads ADD COLUMN status TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('uploading', 'done', 'failed'));
    ALTER TABLE uploads ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE uploads ADD COLUMN error TEXT;
    """,
    # The user edited the post text in the dashboard: a re-render or re-pick must not overwrite it.
    """
    ALTER TABLE clips ADD COLUMN edited INTEGER NOT NULL DEFAULT 0;
    """,
    # The user deleted the VOD in the dashboard (every file but the clip videos): the row stays, keeping its clips,
    # so polling never clips it again.
    """
    ALTER TABLE vods ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0;
    """,
    # A run the user started by hand is using the VOD's files, refreshed like a claim (polling runs hold a claim).
    """
    ALTER TABLE vods ADD COLUMN running_at TEXT;
    """,
    # Runs started from the dashboard (clipit/runs.py), done by their own process so they outlive the dashboard.
    """
    CREATE TABLE runs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        ended_at   TEXT,                       -- NULL while it runs
        beat_at    TEXT NOT NULL,              -- the runner refreshes it while alive: a stale one died
        steps      TEXT NOT NULL,              -- JSON [{"name", "status"}]
        stop       INTEGER NOT NULL DEFAULT 0  -- the user asked it to stop
    );
    """,
    # What a run's step is doing now, as it says (runs.progress): JSON {"what", "vod", "stage", "count"}.
    """
    ALTER TABLE runs ADD COLUMN doing TEXT;
    """,
    # A high-effort edit of a moment (clipit.edit) is a clip of its own next to the plain one: its own id, verdict and
    # uploads. The key gains the variant, so rebuild again. Dropping clips cascades to uploads: they're put back.
    """
    CREATE TABLE uploads_keep AS SELECT * FROM uploads;
    CREATE TABLE clips_new (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        vod_id     TEXT NOT NULL REFERENCES vods (id) ON DELETE CASCADE,
        idx        INTEGER NOT NULL,
        file       TEXT NOT NULL,
        start_s    REAL NOT NULL,
        end_s      REAL NOT NULL,
        title      TEXT NOT NULL,
        hook       TEXT NOT NULL,
        caption    TEXT NOT NULL,
        trend      TEXT NOT NULL,
        sound      TEXT NOT NULL,
        score      REAL NOT NULL,
        why        TEXT NOT NULL,
        created_at TEXT NOT NULL,
        streamer   TEXT NOT NULL DEFAULT '',
        youtube    TEXT NOT NULL DEFAULT '',
        verdict    TEXT CHECK (verdict IN ('keep', 'skip')),
        edited     INTEGER NOT NULL DEFAULT 0,
        variant    TEXT NOT NULL DEFAULT 'plain' CHECK (variant IN ('plain', 'edited')),
        UNIQUE (vod_id, start_s, end_s, variant)
    );
    INSERT INTO clips_new (id, vod_id, idx, file, start_s, end_s, title, hook, caption, trend, sound, score, why, created_at, streamer, youtube, verdict, edited)
        SELECT id, vod_id, idx, file, start_s, end_s, title, hook, caption, trend, sound, score, why, created_at, streamer, youtube, verdict, edited FROM clips ORDER BY id;
    DELETE FROM sqlite_sequence WHERE name = 'clips_new';  -- a deleted clip's id is still never reused
    INSERT INTO sqlite_sequence (name, seq) SELECT 'clips_new', seq FROM sqlite_sequence WHERE name = 'clips';
    DROP TABLE clips;
    ALTER TABLE clips_new RENAME TO clips;
    INSERT INTO uploads SELECT * FROM uploads_keep;
    DROP TABLE uploads_keep;
    """,
    # The dashboard's run settings (runs.SETTINGS), each key's value JSON; what each run was asked to do (JSON: the
    # settings it started with, or a clip to re-edit); each clip as the AI made it (JSON, what a re-edit starts from);
    # and the re-edits the user asked for (clipit --reedit): result_id is the edited clip it made, NULL when it failed.
    """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ALTER TABLE runs ADD COLUMN job TEXT;
    ALTER TABLE clips ADD COLUMN pick TEXT;
    CREATE TABLE reedits (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        clip_id    INTEGER REFERENCES clips (id) ON DELETE SET NULL,
        result_id  INTEGER REFERENCES clips (id) ON DELETE CASCADE,
        request    TEXT NOT NULL,
        at_s       REAL,                 -- the moment the user marked, in seconds of the clip's video
        created_at TEXT NOT NULL,
        edit_score REAL,
        error      TEXT
    );
    """,
]
TEXT_FIELDS = {"title": 100, "youtube": 5000, "caption": 2200}  # what the user can edit, capped at the platforms' limits
PLATFORMS = ("tiktok", "youtube")

FRESH = datetime.timedelta(days=2)  # automatic posts: older clips ride a trend that has passed
AUTO_SCORE = 8  # automatic posts: an unreviewed clip goes up if the clip-picking AI scored it this high (1-10)
STALE_CLAIM = datetime.timedelta(minutes=30)  # a 'processing' row not refreshed for this long was a crashed run
HEARTBEAT = datetime.timedelta(minutes=5)  # how often a live run refreshes its claim, well under STALE_CLAIM
MAX_ATTEMPTS = 3
RETRY_AFTER = datetime.timedelta(hours=1)
RUN_DEAD = datetime.timedelta(minutes=1)  # a run whose runner hasn't beaten for this long died (runs.BEAT is seconds)


def connect(path: Path) -> sqlite3.Connection:
    """Open (and create or upgrade) the database. Autocommit mode: write transactions are explicit."""
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")  # readers (a future dashboard) don't block the poller
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for v, sql in enumerate(MIGRATIONS[version:], version + 1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {v};\nCOMMIT;")
    return conn


def claim(conn: sqlite3.Connection, vod_id: str, url: str, login: str | None, tier: str, now=None) -> bool:
    """Atomically take a VOD for processing. False when it's done, another run has it, or it failed too often."""
    now = now or utcnow()
    conn.execute("BEGIN IMMEDIATE")  # take the write lock before reading, so two pollers can't both claim
    try:
        row = conn.execute("SELECT status, attempts, claimed_at, finished_at FROM vods WHERE id = ?", (vod_id,)).fetchone()
        if row:
            busy = row["status"] == "processing" and now - _ts(row["claimed_at"]) < STALE_CLAIM
            gave_up = row["status"] == "failed" and (
                row["attempts"] >= MAX_ATTEMPTS or now - _ts(row["finished_at"]) < RETRY_AFTER
            )
            if row["status"] == "done" or busy or gave_up:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "UPDATE vods SET status = 'processing', attempts = attempts + 1, error = NULL, claimed_at = ? WHERE id = ?",
                (now.isoformat(), vod_id),
            )
        else:
            conn.execute(
                "INSERT INTO vods (id, url, login, tier, status, attempts, claimed_at) VALUES (?, ?, ?, ?, 'processing', 1, ?)",
                (vod_id, url, login, tier, now.isoformat()),
            )
        conn.execute("COMMIT")
        return True
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@contextlib.contextmanager
def heartbeat(conn: sqlite3.Connection, vod_id: str, manual: bool = False) -> Iterator[None]:
    """Refresh the VOD's claim (manual: its running_at) while the block runs, so a long but alive run is never taken
    for a crashed one."""
    sql = (
        "UPDATE vods SET running_at = ? WHERE id = ? AND running_at IS NOT NULL"
        if manual
        else "UPDATE vods SET claimed_at = ? WHERE id = ? AND status = 'processing'"
    )
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    stop = threading.Event()

    def beat() -> None:
        own = sqlite3.connect(path, timeout=30, isolation_level=None)  # connections can't cross threads
        try:
            while not stop.wait(HEARTBEAT.total_seconds()):
                try:
                    own.execute(sql, (utcnow().isoformat(), vod_id))
                except sqlite3.Error:
                    logger.warning("could not refresh the claim on %s", vod_id, exc_info=True)
        finally:
            own.close()

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()


@contextlib.contextmanager
def running(conn: sqlite3.Connection, vod_id: str) -> Iterator[None]:
    """Mark a run the user started by hand as using the VOD's files, so the dashboard doesn't delete them meanwhile.
    Its status stays as it was: a failed rerun of a done VOD mustn't make the poller retry it."""
    conn.execute("UPDATE vods SET running_at = ? WHERE id = ?", (utcnow().isoformat(), vod_id))
    try:
        with heartbeat(conn, vod_id, manual=True):
            yield
    finally:
        conn.execute("UPDATE vods SET running_at = NULL WHERE id = ?", (vod_id,))


def busy(row: sqlite3.Row, now: datetime.datetime) -> bool:
    """A run is using the VOD's files: a live claim (polling) or a live running_at (a run started by hand)."""
    claimed = row["status"] == "processing" and now - _ts(row["claimed_at"]) < STALE_CLAIM
    return claimed or bool(row["running_at"]) and now - _ts(row["running_at"]) < STALE_CLAIM


def finish(conn: sqlite3.Connection, vod_id: str, error: str | None = None, now=None) -> None:
    conn.execute(
        "UPDATE vods SET status = ?, error = ?, finished_at = ? WHERE id = ?",
        ("failed" if error else "done", error, (now or utcnow()).isoformat(), vod_id),
    )


def record_manual(conn: sqlite3.Connection, vod_id: str, url: str) -> None:
    """A successful run the user started by hand, recorded so uploaders see its clips and polling skips it."""
    now = utcnow().isoformat()
    conn.execute(
        """INSERT INTO vods (id, url, tier, status, attempts, claimed_at, finished_at)
           VALUES (?, ?, 'manual', 'done', 1, ?, ?)
           ON CONFLICT (id) DO UPDATE SET status = 'done', error = NULL, deleted = 0, finished_at = excluded.finished_at""",
        (vod_id, url, now, now),
    )


def status(conn: sqlite3.Connection, vod_id: str) -> str | None:
    row = conn.execute("SELECT status FROM vods WHERE id = ?", (vod_id,)).fetchone()
    return row and row["status"]


def save_clips(conn: sqlite3.Connection, vod_id: str, clips: list[dict]) -> None:
    """Make the VOD's clips of these variants exactly these: plain ones, or high-effort edits (clips with an "edit"),
    so saving one kind never drops the other. Keyed by (vod, start, end, variant): the same moment keeps its clip id
    across re-renders and re-picks, so anything that references it (uploads) stays right."""
    now = utcnow().isoformat()  # one time for the batch: clips made together sort by rank
    clips = [{**c, "variant": variant(c)} for c in clips]
    keep = {(c["start"], c["end"], c["variant"]) for c in clips}
    reedited = "SELECT result_id FROM reedits WHERE result_id IS NOT NULL"  # the user's own re-edit outlives re-picks
    conn.execute("BEGIN IMMEDIATE")
    try:
        # picks the AI dropped are deleted, unless the user kept, posted or re-edited them: those stay in the catalog
        rows = conn.execute(
            f"SELECT id, start_s, end_s, variant, id IN ({reedited}) AS mine FROM clips WHERE vod_id = ?",  # noqa: S608 (a constant)
            (vod_id,),
        ).fetchall()
        for row in rows:
            if row["variant"] in {k[2] for k in keep} and (row["start_s"], row["end_s"], row["variant"]) not in keep:
                conn.execute(
                    f"""DELETE FROM clips WHERE id = ? AND verdict IS NOT 'keep' AND id NOT IN (SELECT clip_id FROM uploads)
                        AND id NOT IN ({reedited})""",  # noqa: S608 (a constant)
                    (row["id"],),
                )
        mine = {(r["start_s"], r["end_s"], r["variant"]) for r in rows if r["mine"]}
        for c in clips:  # the verdict is the user's: a re-render never touches it
            if (c["start"], c["end"], c["variant"]) not in mine:
                _upsert(conn, vod_id, c, now)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def variant(clip: dict) -> str:
    return "edited" if clip.get("edit") else "plain"


def _upsert(conn: sqlite3.Connection, vod_id: str, c: dict, now: str) -> int:
    """Insert or update the clip of this moment and variant, its id. Text the user edited stays theirs."""
    return conn.execute(
        """INSERT INTO clips (vod_id, idx, file, start_s, end_s, title, hook, caption, youtube, streamer, trend, sound,
                              score, why, created_at, variant, pick)
           VALUES (:vod_id, :idx, :file, :start, :end, :title, :hook, :caption, :youtube, :streamer, :trend, :sound,
                   :score, :why, :now, :variant, :pick)
           ON CONFLICT (vod_id, start_s, end_s, variant) DO UPDATE SET idx = excluded.idx, file = excluded.file,
               title = IIF(edited, title, excluded.title), hook = excluded.hook,
               caption = IIF(edited, caption, excluded.caption), youtube = IIF(edited, youtube, excluded.youtube),
               streamer = excluded.streamer, trend = excluded.trend, sound = excluded.sound, score = excluded.score,
               why = excluded.why, pick = excluded.pick
           RETURNING id""",
        {**c, "vod_id": vod_id, "now": now, "variant": variant(c), "pick": json.dumps(c)},
    ).fetchone()[0]


def reedit_source(conn: sqlite3.Connection, clip_id: int) -> dict | None:
    """What a re-edit of the clip needs: the clip, its VOD's link, whether the VOD's files were deleted and whether
    the clip is on YouTube (a posted video can't change). None when there's no such clip."""
    row = conn.execute(
        """SELECT c.id, c.vod_id, c.idx, c.file, c.start_s, c.end_s, c.variant, c.pick, v.url, v.deleted,
                  EXISTS (SELECT 1 FROM uploads WHERE clip_id = c.id AND platform = 'youtube' AND status != 'failed') AS posted
           FROM clips c JOIN vods v ON v.id = c.vod_id WHERE c.id = ?""",
        (clip_id,),
    ).fetchone()
    return row and {**row, "pick": json.loads(row["pick"]) if row["pick"] else None}


def start_reedit(conn: sqlite3.Connection, clip_id: int, request: str, at: float | None) -> int:
    """Record a re-edit as it starts, so one that's killed (Stop, a timeout) still shows: open until it ends."""
    cur = conn.execute(
        "INSERT INTO reedits (clip_id, request, at_s, created_at) VALUES (?, ?, ?, ?)",
        (clip_id, request, at, utcnow().isoformat()),
    )
    return cur.lastrowid or 0


def save_reedit(conn: sqlite3.Connection, reedit_id: int, source: dict, clip: dict) -> tuple[int, str | None] | None:
    """Record a re-edit that worked: the edited clip of the source's moment is now this one (made, or replaced when there
    was one), keeping its id, verdict and the text the user edited. Returns its id and the video it replaced, if any.
    None, changing nothing, when the clip it would replace is on YouTube or going up: its post must keep its video."""
    conn.execute("BEGIN IMMEDIATE")  # an upload claims its clip the same way: it can't start in between
    try:
        old = conn.execute(
            """SELECT file, EXISTS (SELECT 1 FROM uploads WHERE clip_id = clips.id AND status != 'failed') AS posted
               FROM clips WHERE vod_id = ? AND start_s = ? AND end_s = ? AND variant = ?""",
            (source["vod_id"], clip["start"], clip["end"], variant(clip)),
        ).fetchone()
        if old and old["posted"]:
            conn.execute("ROLLBACK")
            return None
        result = _upsert(conn, source["vod_id"], clip, utcnow().isoformat())
        conn.execute("UPDATE reedits SET result_id = ?, edit_score = ? WHERE id = ?", (result, clip.get("edit_score"), reedit_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return result, old["file"] if old and old["file"] != clip["file"] else None


def fail_reedit(conn: sqlite3.Connection, error: str, reedit_id: int | None = None, clip_id: int | None = None) -> None:
    """End the open re-edit (reedit_id), or every open one of a clip (clip_id: its runner, when the step was killed)."""
    conn.execute(
        "UPDATE reedits SET error = ? WHERE (id = ? OR clip_id = ?) AND result_id IS NULL AND error IS NULL",
        (error[:500], reedit_id, clip_id),
    )


def setting(conn: sqlite3.Connection, key: str) -> object:
    """A saved setting, None when it was never saved."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row and json.loads(row["value"])


def save_setting(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def overview(conn: sqlite3.Connection) -> dict:
    """Every VOD and every clip with where it was posted, newest first: what the dashboard shows."""
    uploads: dict[int, dict] = {}
    for r in conn.execute("SELECT clip_id, platform, url, uploaded_at, status FROM uploads WHERE status != 'failed'"):
        uploads.setdefault(r["clip_id"], {})[r["platform"]] = {"url": r["url"], "at": r["uploaded_at"], "status": r["status"]}
    clips = conn.execute(
        """SELECT c.id, c.vod_id, c.idx, c.file, c.start_s, c.end_s, c.title, c.hook, c.caption, c.youtube, c.trend, c.sound,
                  c.score, c.why, c.verdict, c.edited, c.created_at, c.variant, v.url AS vod_url,
                  COALESCE(NULLIF(c.streamer, ''), v.login, '') AS streamer
           FROM clips c JOIN vods v ON v.id = c.vod_id ORDER BY c.created_at DESC, c.idx"""
    )
    vods = conn.execute(
        """SELECT id, url, COALESCE(login, (SELECT streamer FROM clips WHERE vod_id = vods.id AND streamer != ''), '')
                      AS streamer, tier, status, attempts, error, claimed_at, running_at, finished_at
           FROM vods WHERE NOT deleted ORDER BY COALESCE(finished_at, claimed_at) DESC"""
    )
    asked: dict[int, list] = {}  # re-edits, on the clip asked about and on the edited clip they made
    for r in conn.execute("SELECT clip_id, result_id, request, at_s, created_at, error FROM reedits ORDER BY id"):
        for clip in {r["clip_id"], r["result_id"]} - {None}:
            asked.setdefault(clip, []).append({k: r[k] for k in ("request", "at_s", "created_at", "error", "result_id")})
    return {
        "vods": [dict(r) for r in vods],
        "clips": [{**r, "uploads": uploads.get(r["id"], {}), "reedits": asked.get(r["id"], [])} for r in map(dict, clips)],
    }


def clip_file(conn: sqlite3.Connection, clip_id: int) -> str | None:
    """The clip's video, relative to the out folder."""
    row = conn.execute("SELECT file FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return row and row["file"]


@contextlib.contextmanager
def deleting_clip(conn: sqlite3.Connection, clip_id: int, only_skipped: bool = False) -> Iterator[str | None]:
    """Remove the clip and its upload records, yielding its file for the block to delete. All or nothing: if the block
    raises (the file couldn't be deleted), the row stays. Yields None when there's no such clip, it's being uploaded,
    or only_skipped and it isn't skipped (anymore: another tab may have kept it since the list was read)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            """DELETE FROM clips WHERE id = ? AND id NOT IN (SELECT clip_id FROM uploads WHERE status = 'uploading')
                   AND (NOT ? OR verdict = 'skip')
               RETURNING file""",
            (clip_id, only_skipped),
        ).fetchall()
        yield rows[0]["file"] if rows else None
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@contextlib.contextmanager
def deleting_vod(conn: sqlite3.Connection, vod_id: str, now=None) -> Iterator[list[str] | None]:
    """Hide the VOD but keep its clips: the row stays as 'done' and deleted, so its clips keep their VOD and polling
    never clips it again. Yields its clips' videos (relative to the out folder) for the block to delete every other
    file it has; all or nothing, like deleting_clip. Yields None (and changes nothing) when there's no such VOD or a
    run is using its files (db.busy)."""
    now = now or utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status, claimed_at, running_at FROM vods WHERE id = ? AND NOT deleted", (vod_id,)).fetchone()
        ok = bool(row) and not busy(row, now)
        if ok:
            conn.execute("UPDATE vods SET deleted = 1, status = 'done', error = NULL WHERE id = ?", (vod_id,))
        yield [r[0] for r in conn.execute("SELECT file FROM clips WHERE vod_id = ?", (vod_id,))] if ok else None
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def skipped(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM clips WHERE verdict = 'skip'")]


def set_verdict(conn: sqlite3.Connection, clip_id: int, verdict: str | None) -> bool:
    """'keep', 'skip', or None to undo. False when there's no such clip."""
    return conn.execute("UPDATE clips SET verdict = ? WHERE id = ?", (verdict, clip_id)).rowcount == 1


def set_text(conn: sqlite3.Connection, clip_id: int, field: str, text: str) -> bool:
    """The user's own title / YouTube description / TikTok caption, kept from then on. False when there's no such clip."""
    if field not in TEXT_FIELDS:
        raise ValueError(f"not an editable field: {field}")
    # an old clip's empty description meant "use the caption": keep showing that on its first edit. After that the text is
    # the user's as is, so a description they cleared stays empty.
    sql = f"UPDATE clips SET youtube = IIF(NOT edited AND youtube = '', caption, youtube), {field} = ?, edited = 1 WHERE id = ?"  # noqa: S608 (field is checked above)
    return conn.execute(sql, (text, clip_id)).rowcount == 1


def set_upload(conn: sqlite3.Connection, clip_id: int, platform: str, url: str | None) -> bool:
    """Record the clip as posted on platform (url: the post, '' when unknown); url None un-marks it.
    False when there's no such clip."""
    if url is None:
        conn.execute("DELETE FROM uploads WHERE clip_id = ? AND platform = ?", (clip_id, platform))
        return clip_file(conn, clip_id) is not None
    try:
        conn.execute(
            """INSERT INTO uploads (clip_id, platform, url, uploaded_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (clip_id, platform) DO UPDATE SET url = excluded.url, status = 'done', error = NULL,
                   uploaded_at = CASE WHEN status = 'done' THEN uploaded_at ELSE excluded.uploaded_at END""",
            (clip_id, platform, url, utcnow().isoformat()),
        )
    except sqlite3.IntegrityError:  # the clip doesn't exist (the platform is checked by the caller and the schema)
        return False
    return True


def claim_upload(
    conn: sqlite3.Connection,
    platform: str,
    per_day: int,
    gap: datetime.timedelta,
    now: datetime.datetime | None = None,
    first: list[str] | None = None,
) -> sqlite3.Row | None:
    """Atomically take the next clip to post automatically, if the pacing allows: from a VOD polling took in the last FRESH
    (a hand rerun doesn't renew it; clips from old or hand-run VODs are posted only by hand), not on platform yet, kept by the user or unreviewed with
    an AI score of AUTO_SCORE+ (never skipped): clips of the `first` streamers ("site:login") first, then kept, then newest. At most per_day posts in 24 hours, at least gap apart (posts marked by hand count too). None when nothing is due.
    The upload then ends with set_upload (posted) or fail_upload (retried later, up to MAX_ATTEMPTS)."""
    now = now or utcnow()
    conn.execute("BEGIN IMMEDIATE")  # two overlapping runs must not both post, nor both see room in the pacing
    try:
        n, last = conn.execute(
            "SELECT count(*), max(uploaded_at) FROM uploads WHERE platform = ? AND status != 'failed' AND uploaded_at >= ?",
            (platform, (now - datetime.timedelta(days=1)).isoformat()),
        ).fetchone()
        clip = None
        if n < per_day and (last is None or now - _ts(last) >= gap):
            clip = conn.execute(
                """SELECT c.id, c.file, c.title, c.youtube, c.caption, c.edited FROM clips c
                   JOIN vods v ON v.id = c.vod_id
                   LEFT JOIN uploads u ON u.clip_id = c.id AND u.platform = ?
                   WHERE (c.verdict = 'keep' OR c.verdict IS NULL AND c.score >= ?) AND v.tier != 'manual' AND v.claimed_at >= ?
                     AND (u.clip_id IS NULL OR (u.status = 'failed' AND u.attempts < ? AND u.uploaded_at < ?))
                   ORDER BY IIF(v.url LIKE '%kick.com/%', 'kick:', 'twitch:') || lower(v.login) NOT IN (SELECT value FROM json_each(?)), c.verdict IS NULL, c.created_at DESC, c.idx LIMIT 1""",
                (
                    platform,
                    AUTO_SCORE,
                    (now - FRESH).isoformat(),
                    MAX_ATTEMPTS,
                    (now - RETRY_AFTER).isoformat(),
                    json.dumps(first or []),
                ),
            ).fetchone()
        if clip:
            _take_upload(conn, clip["id"], platform, now)
        conn.execute("COMMIT")
        return clip
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def claim_clip_upload(
    conn: sqlite3.Connection, clip_id: int, platform: str, now: datetime.datetime | None = None
) -> sqlite3.Row | None:
    """Atomically take one clip for upload because the user asked for it: kept or not, whatever the pacing.
    None when there's no such clip, or it's posted or being posted already."""
    now = now or utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        clip = conn.execute(
            """SELECT c.id, c.file, c.title, c.youtube, c.caption, c.edited FROM clips c
               LEFT JOIN uploads u ON u.clip_id = c.id AND u.platform = ?
               WHERE c.id = ? AND (u.clip_id IS NULL OR u.status = 'failed')""",
            (platform, clip_id),
        ).fetchone()
        if clip:
            _take_upload(conn, clip_id, platform, now)
        conn.execute("COMMIT")
        return clip
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _take_upload(conn: sqlite3.Connection, clip_id: int, platform: str, now: datetime.datetime) -> None:
    conn.execute(
        """INSERT INTO uploads (clip_id, platform, uploaded_at, status, attempts) VALUES (?, ?, ?, 'uploading', 1)
           ON CONFLICT (clip_id, platform) DO UPDATE SET status = 'uploading', attempts = attempts + 1, error = NULL,
               uploaded_at = excluded.uploaded_at""",
        (clip_id, platform, now.isoformat()),
    )


def fail_upload(
    conn: sqlite3.Connection, clip_id: int, platform: str, error: str, retry: bool = True, spent: bool = True
) -> None:
    """retry False: never claimed automatically again (the post may exist); the user can still post it by hand.
    spent False: the attempt doesn't count toward MAX_ATTEMPTS (the platform's daily limit, not the clip, was the problem)."""
    conn.execute(
        """UPDATE uploads SET status = 'failed', error = ?, attempts = IIF(?, attempts - NOT ?, MAX(attempts, ?))
           WHERE clip_id = ? AND platform = ? AND status = 'uploading'""",
        (error, retry, spent, MAX_ATTEMPTS, clip_id, platform),
    )


def stuck_uploads(conn: sqlite3.Connection, platform: str, before: datetime.datetime) -> list[int]:
    """Clips claimed for upload before `before` that never finished: the run died mid-upload, so the post may exist."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT clip_id FROM uploads WHERE platform = ? AND status = 'uploading' AND uploaded_at < ?",
            (platform, before.isoformat()),
        )
    ]


def _ts(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


def start_run(
    conn: sqlite3.Connection, steps: list[str], job: dict | None = None, now: datetime.datetime | None = None
) -> int | None:
    """Record a new run of the job (runs.steps), all its steps waiting. None when a live run is going: one at a time."""
    now = now or utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if (last := latest_run(conn, now)) and last["running"]:
            conn.execute("ROLLBACK")
            return None
        cur = conn.execute(
            "INSERT INTO runs (started_at, beat_at, steps, job) VALUES (?, ?, ?, ?)",
            (now.isoformat(), now.isoformat(), json.dumps([{"name": n, "status": "waiting"} for n in steps]), json.dumps(job)),
        )
        conn.execute("COMMIT")
        return cur.lastrowid
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def beat_run(conn: sqlite3.Connection, run_id: int, steps: list[dict]) -> bool:
    """The runner is alive and its steps are these. True when the user asked it to stop."""
    conn.execute("UPDATE runs SET beat_at = ?, steps = ? WHERE id = ?", (utcnow().isoformat(), json.dumps(steps), run_id))
    return bool(conn.execute("SELECT stop FROM runs WHERE id = ?", (run_id,)).fetchone()["stop"])


def end_run(conn: sqlite3.Connection, run_id: int, steps: list[dict]) -> None:
    now = utcnow().isoformat()
    conn.execute("UPDATE runs SET beat_at = ?, ended_at = ?, steps = ? WHERE id = ?", (now, now, json.dumps(steps), run_id))


def run_doing(conn: sqlite3.Connection, run_id: int, doing: dict, replace: bool = False) -> None:
    """What the run is doing now, for the page: merged into what it said before (a None value removes that field)."""
    sql = (
        "UPDATE runs SET doing = ? WHERE id = ?"
        if replace
        else "UPDATE runs SET doing = json_patch(coalesce(doing, '{}'), ?) WHERE id = ?"
    )
    conn.execute(sql, (json.dumps(doing), run_id))


def stop_run(conn: sqlite3.Connection) -> bool:
    """Ask the live run to stop. False when there is none."""
    last = latest_run(conn)
    if not last or not last["running"]:
        return False
    conn.execute("UPDATE runs SET stop = 1 WHERE id = ?", (last["id"],))
    return True


def latest_run(conn: sqlite3.Connection, now: datetime.datetime | None = None) -> dict | None:
    """The newest run as the page shows it. A runner that stopped beating died (PC restart, killed): its run shows as
    ended where it last beat, its unfinished steps interrupted."""
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    steps = json.loads(row["steps"])
    ended = row["ended_at"]
    if not ended and (now or utcnow()) - _ts(row["beat_at"]) > RUN_DEAD:
        ended = row["beat_at"]
        steps = [{**s, "status": "interrupted"} if s["status"] in ("waiting", "running") else s for s in steps]
    return {"id": row["id"], "running": not ended, "stopping": bool(row["stop"]) and not ended,
            "started": row["started_at"], "ended": ended, "steps": steps,
            "doing": json.loads(row["doing"] or "{}") if not ended else {},
            "job": json.loads(row["job"] or "null")}  # fmt: skip


def run_job(conn: sqlite3.Connection, run_id: int) -> dict | None:
    """What the run was asked to do, None for a run from before jobs (it polled, then posted)."""
    row = conn.execute("SELECT job FROM runs WHERE id = ?", (run_id,)).fetchone()
    return row and json.loads(row["job"] or "null")
