"""Command line entry point.

clipit https://www.twitch.tv/videos/2906456464
clipit URL --clips 10 --ai "ollama run qwen3:30b"
clipit --refresh-trends
clipit --poll              (clip the latest VODs of trending, big and smaller streamers; run it on a timer)
clipit --poll --dry-run    (only show which VODs it would clip)
clipit --dashboard         (browse every VOD and clip, mark clips keep/skip and posted)
clipit --youtube-login     (once: sign in to the YouTube channel to post to)
clipit --poll --upload     (also post the newest clip you kept to YouTube Shorts, a few a day)
"""

import argparse
import io
import logging
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from clipit import ai, dashboard, db, poll, youtube
from clipit.pipeline import process_vod
from clipit.util import ROOT, ClipitError, log, logger


def setup_logging(out: Path) -> None:
    """Console for humans, plus a rotating file with timestamps and tracebacks for unattended runs."""
    (out / "logs").mkdir(parents=True, exist_ok=True)
    file = RotatingFileHandler(out / "logs" / "clipit.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("[clip-it] %(message)s"))
    logger.setLevel(logging.INFO)
    logger.addHandler(file)
    logger.addHandler(console)


def refresh_stale_trends(trends_ai: str | None) -> None:
    """Redo trend research older than ai.TRENDS_MAX_AGE, so polling clips who everyone is talking about now."""
    age = ai.trends_age()
    if not trends_ai or (age is not None and age <= ai.TRENDS_MAX_AGE):
        return
    log("trend research is stale, redoing it so polling clips who everyone is talking about...")
    try:
        ai.refresh_trends(trends_ai)
    except Exception:  # polling still runs, on the older list or by viewers
        logger.exception("trend research failed")


def main() -> None:
    ap = argparse.ArgumentParser(prog="clipit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="Twitch or Kick VOD link (other yt-dlp sites work too, without chat)")
    ap.add_argument("-n", "--clips", type=int, default=6, help="how many clips to make per VOD (default 6)")
    ap.add_argument(
        "--ai",
        default=ai.default_ai(),
        help="'codex' or a command that reads a prompt on stdin and prints the answer, used to pick clips. "
        "Give custom commands no tools: prompts contain text from strangers. Default: Claude Code, then Codex if installed.",
    )
    ap.add_argument(
        "--trends-ai",
        default=ai.default_trends_ai(),
        help="'codex' or a command for --refresh-trends; needs web search (default: Claude Code, then Codex)",
    )
    ap.add_argument("--whisper", default="turbo", help="faster-whisper model: tiny, base, small, medium, turbo (default)")
    ap.add_argument("--out", type=Path, default=ROOT / "out", help="working files, clips, logs and the database")
    ap.add_argument(
        "--refresh-trends", action="store_true", help="have the AI research current TikTok/Shorts trends into trends.md"
    )
    ap.add_argument("--poll", action="store_true", help="pick live streamers automatically and clip their latest finished VOD")
    ap.add_argument(
        "--top", type=int, default=6, help="--poll: how many trending, then biggest live, streamers to clip (default 6)"
    )
    ap.add_argument(
        "--small",
        type=int,
        default=4,
        help=f"--poll: max VODs per day from smaller streamers, {poll.SMALL[0]}-{poll.SMALL[1]} viewers (default 4)",
    )
    ap.add_argument("--lang", default="EN", help='--poll: only streams in this language (default EN, "" for any)')
    ap.add_argument("--dry-run", action="store_true", help="--poll: only print which VODs would be clipped")
    ap.add_argument("--dashboard", action="store_true", help="open the local dashboard of every VOD and clip")
    ap.add_argument("--port", type=int, default=8765, help="--dashboard: port on 127.0.0.1 (default 8765)")
    ap.add_argument("--youtube-login", action="store_true", help="sign in to the YouTube channel to post to (once)")
    ap.add_argument(
        "--upload",
        action="store_true",
        help=f"post the newest clip you marked Keep to YouTube Shorts, at most {youtube.PER_DAY} a day and "
        f"{youtube.GAP.seconds // 3600}h apart. Add it to the --poll timer",
    )
    a = ap.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):  # not when stdout was swapped (tests, some service wrappers)
        sys.stdout.reconfigure(errors="replace")  # emoji in stream titles crash cp1252 consoles
    if not any((a.url, a.refresh_trends, a.poll, a.dashboard, a.youtube_login, a.upload)):
        ap.error("pass a VOD url (or --poll / --upload / --youtube-login / --refresh-trends / --dashboard)")
    if (a.url or a.poll) and not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    setup_logging(a.out)
    conn = db.connect(a.out / "clipit.db")
    try:
        if a.poll and not a.dry_run and not a.refresh_trends:  # --refresh-trends does it below, a dry run touches nothing
            refresh_stale_trends(a.trends_ai)
        if a.refresh_trends:
            if not a.trends_ai:
                raise ClipitError('No AI for trend research. Install Claude Code or Codex CLI, or pass --trends-ai "<command>"')
            log("researching current trends (web search)...")
            ai.refresh_trends(a.trends_ai)
            log(f"wrote {ai.TRENDS}")
        if a.youtube_login:
            youtube.login()
        if a.upload and a.dry_run:
            log("dry run: not posting to YouTube")
        elif a.upload:
            try:
                youtube.upload_next(conn, a.out)
            except Exception:  # logged, and retried on a later run: the rest of this run (polling) still happens
                logger.exception("YouTube upload failed")
        if a.url:
            work, clips = process_vod(a.url, a.clips, a.ai, a.whisper, a.out)
            db.record_manual(conn, work.name, a.url)
            db.save_clips(conn, work.name, [{**c, "file": f"{work.name}/{c['file']}"} for c in clips])
        if a.poll:
            poll.poll(conn, a.top, a.small, a.lang.upper(), a.dry_run, a.out, n_clips=a.clips, ai_cmd=a.ai, whisper=a.whisper)
        if a.dashboard:
            dashboard.serve(a.out, a.port)
    except ClipitError as e:
        logger.error("%s", e)
        sys.exit(1)
    finally:
        conn.close()
