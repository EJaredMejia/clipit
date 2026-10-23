"""SQLite catalog: which VODs were processed (the polling job queue) and which clips exist (what uploaders read).

Per-VOD working files (audio, transcripts, renders) stay on disk in out/<vod-id>/ as a cache.
Everything shared across runs lives here, never in ad-hoc JSON files.
"""

import contextlib
import datetime
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
]

STALE_CLAIM = datetime.timedelta(minutes=30)  # a 'processing' row not refreshed for this long was a crashed run
HEARTBEAT = datetime.timedelta(minutes=5)  # how often a live run refreshes its claim, well under STALE_CLAIM
MAX_ATTEMPTS = 3
RETRY_AFTER = datetime.timedelta(hours=1)


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
def heartbeat(conn: sqlite3.Connection, vod_id: str) -> Iterator[None]:
    """Refresh the VOD's claim while the block runs, so a long but alive run is never taken for a crashed one."""
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    stop = threading.Event()

    def beat() -> None:
        own = sqlite3.connect(path, timeout=30, isolation_level=None)  # connections can't cross threads
        try:
            while not stop.wait(HEARTBEAT.total_seconds()):
                try:
                    own.execute(
                        "UPDATE vods SET claimed_at = ? WHERE id = ? AND status = 'processing'", (utcnow().isoformat(), vod_id)
                    )
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
           ON CONFLICT (id) DO UPDATE SET status = 'done', error = NULL, finished_at = excluded.finished_at""",
        (vod_id, url, now, now),
    )


def status(conn: sqlite3.Connection, vod_id: str) -> str | None:
    row = conn.execute("SELECT status FROM vods WHERE id = ?", (vod_id,)).fetchone()
    return row and row["status"]


def save_clips(conn: sqlite3.Connection, vod_id: str, clips: list[dict]) -> None:
    """Make the VOD's clips exactly these. Keyed by (vod, start, end): the same moment keeps its clip id across
    re-renders and re-picks, so anything that references it (uploads) stays right."""
    now = utcnow().isoformat()
    keep = {(c["start"], c["end"]) for c in clips}
    conn.execute("BEGIN IMMEDIATE")
    try:
        # shortcut: picks the AI dropped are deleted; once an uploads table references clips, keep uploaded ones instead
        for row in conn.execute("SELECT id, start_s, end_s FROM clips WHERE vod_id = ?", (vod_id,)).fetchall():
            if (row["start_s"], row["end_s"]) not in keep:
                conn.execute("DELETE FROM clips WHERE id = ?", (row["id"],))
        for c in clips:
            conn.execute(
                """INSERT INTO clips (vod_id, idx, file, start_s, end_s, title, hook, caption, trend, sound, score, why, created_at)
                   VALUES (:vod_id, :idx, :file, :start, :end, :title, :hook, :caption, :trend, :sound, :score, :why, :now)
                   ON CONFLICT (vod_id, start_s, end_s) DO UPDATE SET idx = excluded.idx, file = excluded.file,
                       title = excluded.title, hook = excluded.hook, caption = excluded.caption,
                       trend = excluded.trend, sound = excluded.sound, score = excluded.score, why = excluded.why""",
                {**c, "vod_id": vod_id, "now": now},
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def small_done_since(conn: sqlite3.Connection, since: datetime.datetime) -> int:
    return conn.execute(
        "SELECT count(*) FROM vods WHERE tier = 'small' AND status = 'done' AND finished_at >= ?", (since.isoformat(),)
    ).fetchone()[0]


def _ts(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)
