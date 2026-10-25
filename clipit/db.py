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
]
TEXT_FIELDS = {"title": 100, "youtube": 5000, "caption": 2200}  # what the user can edit, capped at the platforms' limits
PLATFORMS = ("tiktok", "youtube")

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
        # picks the AI dropped are deleted, unless the user kept or posted them: those stay in the catalog
        for row in conn.execute("SELECT id, start_s, end_s FROM clips WHERE vod_id = ?", (vod_id,)).fetchall():
            if (row["start_s"], row["end_s"]) not in keep:
                conn.execute(
                    "DELETE FROM clips WHERE id = ? AND verdict IS NOT 'keep' AND id NOT IN (SELECT clip_id FROM uploads)",
                    (row["id"],),
                )
        for c in clips:  # the verdict is the user's: a re-render never touches it
            conn.execute(
                """INSERT INTO clips (vod_id, idx, file, start_s, end_s, title, hook, caption, youtube, streamer, trend, sound,
                                      score, why, created_at)
                   VALUES (:vod_id, :idx, :file, :start, :end, :title, :hook, :caption, :youtube, :streamer, :trend, :sound,
                           :score, :why, :now)
                   ON CONFLICT (vod_id, start_s, end_s) DO UPDATE SET idx = excluded.idx, file = excluded.file,
                       title = IIF(edited, title, excluded.title), hook = excluded.hook,
                       caption = IIF(edited, caption, excluded.caption), youtube = IIF(edited, youtube, excluded.youtube),
                       streamer = excluded.streamer, trend = excluded.trend, sound = excluded.sound, score = excluded.score,
                       why = excluded.why""",
                {**c, "vod_id": vod_id, "now": now},
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def overview(conn: sqlite3.Connection) -> dict:
    """Every VOD and every clip with where it was posted, newest first: what the dashboard shows."""
    uploads: dict[int, dict] = {}
    for r in conn.execute("SELECT clip_id, platform, url, uploaded_at, status FROM uploads WHERE status != 'failed'"):
        uploads.setdefault(r["clip_id"], {})[r["platform"]] = {"url": r["url"], "at": r["uploaded_at"], "status": r["status"]}
    clips = conn.execute(
        """SELECT c.id, c.vod_id, c.idx, c.file, c.start_s, c.end_s, c.title, c.hook, c.caption, c.youtube, c.trend, c.sound,
                  c.score, c.why, c.verdict, c.created_at, v.url AS vod_url,
                  COALESCE(NULLIF(c.streamer, ''), v.login, '') AS streamer
           FROM clips c JOIN vods v ON v.id = c.vod_id ORDER BY c.created_at DESC, c.idx"""
    )
    vods = conn.execute(
        """SELECT id, url, COALESCE(login, (SELECT streamer FROM clips WHERE vod_id = vods.id AND streamer != ''), '')
                      AS streamer, tier, status, attempts, error, claimed_at, finished_at
           FROM vods ORDER BY COALESCE(finished_at, claimed_at) DESC"""
    )
    return {
        "vods": [dict(r) for r in vods],
        "clips": [{**r, "uploads": uploads.get(r["id"], {})} for r in map(dict, clips)],
    }


def clip_file(conn: sqlite3.Connection, clip_id: int) -> str | None:
    """The clip's video, relative to the out folder."""
    row = conn.execute("SELECT file FROM clips WHERE id = ?", (clip_id,)).fetchone()
    return row and row["file"]


def set_verdict(conn: sqlite3.Connection, clip_id: int, verdict: str | None) -> bool:
    """'keep', 'skip', or None to undo. False when there's no such clip."""
    return conn.execute("UPDATE clips SET verdict = ? WHERE id = ?", (verdict, clip_id)).rowcount == 1


def set_text(conn: sqlite3.Connection, clip_id: int, field: str, text: str) -> bool:
    """The user's own title / YouTube description / TikTok caption, kept from then on. False when there's no such clip."""
    if field not in TEXT_FIELDS:
        raise ValueError(f"not an editable field: {field}")
    return conn.execute(f"UPDATE clips SET {field} = ?, edited = 1 WHERE id = ?", (text, clip_id)).rowcount == 1  # noqa: S608 (field is checked above)


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


def claim_upload(conn: sqlite3.Connection, platform: str, per_day: int, gap: datetime.timedelta, now=None) -> sqlite3.Row | None:
    """Atomically take the newest clip the user kept that isn't on platform yet, if the pacing allows: at most per_day
    posts in 24 hours, at least gap apart (posts marked by hand count too). None when nothing is due.
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
                """SELECT c.id, c.file, c.title, c.youtube, c.caption FROM clips c
                   LEFT JOIN uploads u ON u.clip_id = c.id AND u.platform = ?
                   WHERE c.verdict = 'keep'
                     AND (u.clip_id IS NULL OR (u.status = 'failed' AND u.attempts < ? AND u.uploaded_at < ?))
                   ORDER BY c.created_at DESC, c.idx LIMIT 1""",
                (platform, MAX_ATTEMPTS, (now - RETRY_AFTER).isoformat()),
            ).fetchone()
        if clip:
            _take_upload(conn, clip["id"], platform, now)
        conn.execute("COMMIT")
        return clip
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def claim_clip_upload(conn: sqlite3.Connection, clip_id: int, platform: str, now=None) -> sqlite3.Row | None:
    """Atomically take one clip for upload because the user asked for it: kept or not, whatever the pacing.
    None when there's no such clip, or it's posted or being posted already."""
    now = now or utcnow()
    conn.execute("BEGIN IMMEDIATE")
    try:
        clip = conn.execute(
            """SELECT c.id, c.file, c.title, c.youtube, c.caption FROM clips c
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


def fail_upload(conn: sqlite3.Connection, clip_id: int, platform: str, error: str) -> None:
    conn.execute(
        "UPDATE uploads SET status = 'failed', error = ? WHERE clip_id = ? AND platform = ? AND status = 'uploading'",
        (error, clip_id, platform),
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


def small_done_since(conn: sqlite3.Connection, since: datetime.datetime) -> int:
    return conn.execute(
        "SELECT count(*) FROM vods WHERE tier = 'small' AND status = 'done' AND finished_at >= ?", (since.isoformat(),)
    ).fetchone()[0]


def _ts(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)
