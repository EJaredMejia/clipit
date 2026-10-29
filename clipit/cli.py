"""Command line entry point.

clipit https://www.twitch.tv/videos/2906456464
clipit URL --clips 10 --ai "ollama run qwen3:30b"
clipit --refresh-trends
clipit URL --high-effort   (the AI edits every clip, then reviews and improves it until it's really good)
clipit --reedit 42 --request "a boom when he slams the desk" --at 12.5   (the AI edits clip 42 again, as asked)
clipit --poll              (clip the latest VODs of trending and big streamers; run it on a timer)
clipit --poll --dry-run    (only show which VODs it would clip)
clipit --dashboard         (browse every VOD and clip, mark clips keep/skip and posted)
clipit --youtube-login     (once: sign in to the YouTube channel to post to)
clipit --poll --upload     (also post the best fresh clip to YouTube Shorts, a few a day)
clipit --poll --upload --every 45   (keep running: one pass every 45 minutes, no Task Scheduler needed)
"""

import argparse
import io
import logging
import math
import os
import shutil
import sqlite3
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from clipit import ai, dashboard, db, edit, pipeline, poll, runs, source, youtube
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
    ap.add_argument(
        "--high-effort",
        action="store_true",
        help="much more AI effort per clip: the AI edits it (jump cuts, zooms, sound effects, pop-up text) and reviews "
        f"each version until it scores {edit.GOAL}/10, up to {edit.MAX_ROUNDS} AI reviews per clip. Slow",
    )
    ap.add_argument(
        "--reedit",
        type=int,
        metavar="CLIP_ID",
        help="have the AI edit this clip again until it does what --request asks (the clip's id in the dashboard)",
    )
    ap.add_argument("--request", default="", help=f"--reedit: what to change, up to {runs.REQUEST_MAX} characters")
    ap.add_argument("--at", type=float, metavar="SECONDS", help="--reedit: the second of the clip's video you mean")
    ap.add_argument("--whisper", default="turbo", help="faster-whisper model: tiny, base, small, medium, turbo (default)")
    ap.add_argument("--out", type=Path, default=ROOT / "out", help="working files, clips, logs and the database")
    ap.add_argument(
        "--refresh-trends", action="store_true", help="have the AI research current TikTok/Shorts trends into trends.md"
    )
    ap.add_argument("--poll", action="store_true", help="pick live streamers automatically and clip their latest finished VOD")
    ap.add_argument(
        "--top", type=int, default=6, help="--poll: how many trending, then biggest live, streamers to clip (default 6)"
    )
    ap.add_argument("--lang", default="EN", help='--poll: only streams in this language (default EN, "" for any)')
    ap.add_argument("--dry-run", action="store_true", help="--poll: only print which VODs would be clipped")
    ap.add_argument(
        "--prioritize",
        action="append",
        default=[],
        metavar="SITE:NAME",
        help="--poll / --upload: clip this streamer's latest VOD first and post their fresh clips first, e.g. "
        "twitch:caedrel while an event they cover is big (repeatable)",
    )
    ap.add_argument("--dashboard", action="store_true", help="open the local dashboard of every VOD and clip")
    ap.add_argument("--port", type=int, default=8765, help="--dashboard: port on 127.0.0.1 (default 8765)")
    ap.add_argument("--youtube-login", action="store_true", help="sign in to the YouTube channel to post to (once)")
    ap.add_argument(
        "--upload",
        action="store_true",
        help=f"post a fresh clip you kept, or the AI scored {db.AUTO_SCORE}+, to YouTube Shorts, at most {youtube.PER_DAY} a day and "
        f"{youtube.GAP.seconds // 3600}h apart. Add it to the --poll timer",
    )
    ap.add_argument(
        "--every",
        type=float,
        metavar="MINUTES",
        help="--poll / --upload: keep running, one pass every MINUTES (a failed pass is logged and the next one still runs)",
    )
    ap.add_argument("--run-steps", type=int, help=argparse.SUPPRESS)  # the runner of a run started in the dashboard
    a = ap.parse_args()
    if bad := [p for p in a.prioritize if not ai.TRENDING.fullmatch(f"- {p}")]:
        ap.error(f"--prioritize needs twitch:name or kick:name, not {bad[0]!r}")
    found = [ai.TRENDING.fullmatch(f"- {p}") for p in a.prioritize]
    a.prioritize = [f"{m[1]}:{m[2]}".lower() for m in found if m]  # just the parts, as `- ` and backticks match too
    if a.every is not None:
        if not 0 < a.every < math.inf:  # also false for nan
            ap.error("--every needs a positive number of minutes")
        if not (a.poll or a.upload) or any((a.url, a.dashboard, a.youtube_login, a.refresh_trends)):
            ap.error("--every only repeats --poll and --upload")
    if a.reedit is not None and not runs.reedit_job(a.reedit, a.request, a.at):
        ap.error(f"--reedit needs a --request of up to {runs.REQUEST_MAX} characters, and --at from 0 to 600 seconds")
    if isinstance(sys.stdout, io.TextIOWrapper):  # not when stdout was swapped (tests, some service wrappers)
        sys.stdout.reconfigure(errors="replace")  # emoji in stream titles crash cp1252 consoles
    if not any((a.url, a.refresh_trends, a.poll, a.dashboard, a.youtube_login, a.upload, a.run_steps, a.reedit is not None)):
        ap.error("pass a VOD url (or --poll / --upload / --youtube-login / --refresh-trends / --dashboard)")
    if (a.url or a.poll or a.reedit is not None) and not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    setup_logging(a.out)
    if runner := os.environ.get(runs.WATCH):  # a clipping step of a dashboard run: never outlive the runner
        runs.watch_runner(int(runner))
    conn = db.connect(a.out / "clipit.db")
    runs.reporting(conn)  # a step of a dashboard run: say what it's doing
    try:
        if a.every:
            while True:
                try:
                    run(a, conn)
                except Exception:  # unattended: one bad pass must not stop the loop
                    logger.exception("pass failed")
                log(f"next pass in {a.every:g} min")
                time.sleep(a.every * 60)
        run(a, conn)
    except ClipitError as e:
        logger.error("%s", e)
        sys.exit(1)
    finally:
        conn.close()


def run(a: argparse.Namespace, conn: sqlite3.Connection) -> None:
    """One run of everything the flags ask for."""
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
    upload_failed = False
    if a.upload and a.dry_run:
        log("dry run: not posting to YouTube")
    elif a.upload:
        try:
            youtube.upload_next(conn, a.out, a.prioritize)
        except Exception:  # logged, and retried on a later run: the rest of this run (polling) still happens
            logger.exception("YouTube upload failed")
            upload_failed = True
    if a.url:
        info = source.fetch_info(a.url)  # its id first, to mark the VOD's files in use (the dashboard can delete them)
        with db.running(conn, str(info["id"])):
            work, clips = process_vod(a.url, a.clips, a.ai, a.whisper, a.out, info=info, high_effort=a.high_effort)
        db.record_manual(conn, work.name, a.url)
        db.save_clips(conn, work.name, [{**c, "file": f"{work.name}/{c['file']}"} for c in clips])
    vods_failed = 0
    if a.poll:
        vods_failed = poll.poll(
            conn,
            a.top,
            a.lang.upper(),
            a.dry_run,
            a.out,
            prioritize=[(site, name) for site, name in (p.split(":", 1) for p in a.prioritize)],
            n_clips=a.clips,
            ai_cmd=a.ai,
            whisper=a.whisper,
            high_effort=a.high_effort,
        )
    if a.reedit is not None:
        reedit(conn, a.out, a.reedit, a.request.strip(), a.at, a.ai)
    if a.dashboard:
        dashboard.serve(a.out, a.port)
    if a.run_steps:
        runs.run(conn, a.out, a.run_steps)
    # once the rest has run: the exit code tells a dashboard run (and any scheduler) the work failed
    if upload_failed:
        raise ClipitError("posting to YouTube failed (see above)")
    if vods_failed:
        raise ClipitError(f"{vods_failed} VOD{'s' if vods_failed > 1 else ''} failed to clip (see above)")


def reedit(conn: sqlite3.Connection, out: Path, clip_id: int, request: str, at: float | None, ai_cmd: str | None) -> None:
    """Edit one clip again as the user asked, and record it, done or failed, for the dashboard."""
    clip = db.reedit_source(conn, clip_id)
    if not clip:
        raise ClipitError(f"there's no clip {clip_id}")
    run = os.environ.get(runs.RUN)  # a step of a dashboard run: its runner closes this re-edit if it's killed
    reedit_id = db.start_reedit(conn, clip_id, request, at, int(run) if run else None)
    try:
        if clip["posted"]:
            raise ClipitError("this clip is on YouTube already, and a re-edit can't change a posted video")
        if clip["deleted"]:
            raise ClipitError("this clip's VOD files were deleted, so it can't be re-edited")
        with db.running(conn, clip["vod_id"]):  # the dashboard mustn't delete the VOD's files meanwhile
            new = pipeline.reedit(clip["url"], clip, request, at, ai_cmd, out)
        saved = db.save_reedit(conn, reedit_id, clip, new)
        if not saved:
            (out / new["file"]).unlink(missing_ok=True)
            raise ClipitError("the edited clip of this moment is on YouTube, and a re-edit can't change a posted video")
    except Exception as e:  # shown with the clip in the dashboard, then the run fails
        db.fail_reedit(conn, str(e) or type(e).__name__, reedit_id=reedit_id)
        raise
    result, old = saved
    if old:
        try:
            (out / old).unlink(missing_ok=True)
        except OSError:  # playing in the dashboard (Windows locks open files): it's only a leftover in the cache
            logger.warning("couldn't delete the re-edit's old video %s", old)
    log(f"re-edited: clip {result} is now {new['file']} (edit {new.get('edit_score', 0):g}/10)")
