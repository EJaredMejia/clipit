"""Command line entry point.

clipit https://www.twitch.tv/videos/2906456464
clipit URL --clips 10 --ai "ollama run qwen3:30b"
clipit --refresh-trends
clipit --poll              (clip the latest VODs of big + smaller live streamers; run it on a timer)
clipit --poll --dry-run    (only show which VODs it would clip)
clipit --dashboard         (browse every VOD and clip, mark clips keep/skip and posted)
"""

import argparse
import io
import logging
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from clipit import ai, dashboard, db, poll
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


def main() -> None:
    ap = argparse.ArgumentParser(prog="clipit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", help="Twitch VOD link (other yt-dlp sites work too, without chat)")
    ap.add_argument("-n", "--clips", type=int, default=6, help="how many clips to make per VOD (default 6)")
    ap.add_argument(
        "--ai",
        default=ai.default_ai(),
        help="command that reads a prompt on stdin and prints the answer, used to pick clips. "
        'Give it no tools: its prompt contains text from strangers. Default: Claude Code with --tools ""',
    )
    ap.add_argument(
        "--trends-ai",
        default=ai.default_trends_ai(),
        help="command for --refresh-trends; needs web search (default: Claude Code with web tools only)",
    )
    ap.add_argument("--whisper", default="small", help="faster-whisper model: tiny, base, small (default), medium, turbo")
    ap.add_argument("--out", type=Path, default=ROOT / "out", help="working files, clips, logs and the database")
    ap.add_argument(
        "--refresh-trends", action="store_true", help="have the AI research current TikTok/Shorts trends into trends.md"
    )
    ap.add_argument("--poll", action="store_true", help="pick live streamers automatically and clip their latest finished VOD")
    ap.add_argument("--top", type=int, default=6, help="--poll: how many of the biggest live streamers to clip (default 6)")
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
    a = ap.parse_args()
    if isinstance(sys.stdout, io.TextIOWrapper):  # not when stdout was swapped (tests, some service wrappers)
        sys.stdout.reconfigure(errors="replace")  # emoji in stream titles crash cp1252 consoles
    if not a.url and not a.refresh_trends and not a.poll and not a.dashboard:
        ap.error("pass a VOD url (or --poll / --refresh-trends / --dashboard)")
    if (a.url or a.poll) and not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    setup_logging(a.out)
    conn = db.connect(a.out / "clipit.db")
    try:
        if a.refresh_trends:
            if not a.trends_ai:
                raise ClipitError('No AI for trend research. Install Claude Code or pass --trends-ai "<command>"')
            log("researching current trends (web search)...")
            ai.refresh_trends(a.trends_ai)
            log(f"wrote {ai.TRENDS}")
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
