"""Runs started from the dashboard: `clipit --poll`, then `clipit --upload`, done by a runner process of their own.

The dashboard only starts the runner (start) and reads how it goes: its row in the runs table (db.latest_run) and its
output, out/logs/run-<id>.log. So a run goes on, and shows again, when the dashboard is closed and reopened.

Stop (db.stop_run) kills a clipping step with every process it started (ffmpeg, the AI CLI), but lets a YouTube post
finish: one killed mid-upload may be on YouTube without clipit knowing, and is never retried on its own.
"""

import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

from clipit import db
from clipit.util import logger

# (name, flag, timeout in seconds). Clip first, so this run's fresh clips can be posted in it. A poll of a few
# hours-long VODs takes hours; an upload is one video. A hung step is killed.
STEPS = (("Clip the latest VODs", "--poll", 8 * 3600), ("Post to YouTube", "--upload", 3600))
UPLOAD = "--upload"  # the step Stop never cuts
BEAT = 3.0  # seconds between the runner's checks: alive, stop asked, timed out
KILL_WAIT = 30  # seconds a killed step gets to be gone
WATCH = "CLIPIT_RUNNER"  # a clipping step gets its runner's pid, to end itself when the runner is gone (watch_runner)
RUN = "CLIPIT_RUN"  # a step gets its run's id, to say what it's doing (progress)
LOG_TAIL = 256 * 1024  # bytes of the run's output the page gets: a poll can print megabytes


def log_file(out: Path, run_id: int) -> Path:
    return out / "logs" / f"run-{run_id}.log"


def command(flag: str, out: Path) -> list[str]:
    return [sys.executable, "-m", "clipit", flag, "--out", str(out)]


def start(conn: sqlite3.Connection, out: Path) -> int | None:
    """Record a run and start its runner in the background, apart from the dashboard. None when one is going."""
    run_id = db.start_run(conn, [name for name, _, _ in STEPS])
    if run_id is None:
        return None
    kw: dict  # declared outside the branches: pyright skips the branch that isn't for its platform
    if sys.platform == "win32":
        # no console window, and Ctrl+C in the dashboard's terminal doesn't reach it
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        kw = {"start_new_session": True}
    try:
        # no timeout here: the runner ends on its own, its steps have watchdogs
        subprocess.Popen(
            [sys.executable, "-m", "clipit", "--run-steps", str(run_id), "--out", str(out)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw,
        )  # fmt: skip
    except OSError:
        db.end_run(conn, run_id, [{"name": name, "status": "failed"} for name, _, _ in STEPS])
        raise
    return run_id


def tail(out: Path, run_id: int, lines: int = 1000) -> list[str]:
    """The last lines of the run's output."""
    try:
        with log_file(out, run_id).open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - LOG_TAIL))
            text = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    return text.splitlines()[-lines:]


def run(conn: sqlite3.Connection, out: Path, run_id: int) -> None:
    """The runner (clipit --run-steps): every step in order, a failed one doesn't stop the next, Stop skips the rest."""
    steps = [{"name": name, "status": "waiting"} for name, _, _ in STEPS]
    log_file(out, run_id).parent.mkdir(parents=True, exist_ok=True)
    with log_file(out, run_id).open("a", encoding="utf-8") as log:
        try:
            for step, (_, flag, timeout) in zip(steps, STEPS, strict=True):
                if db.beat_run(conn, run_id, steps):
                    step["status"] = "stopped"
                    continue
                step["status"] = "running"
                db.run_doing(conn, run_id, {"what": step["name"]}, replace=True)  # nothing left from the step before
                _say(log, f"{step['name']}: clipit {flag}")
                step["status"] = _step(conn, run_id, steps, command(flag, out), log, timeout, stoppable=flag != UPLOAD)
        except Exception as e:  # the run must still end, or the page shows it running until it goes stale
            logger.exception("run %d failed", run_id)
            _say(log, f"run failed: {type(e).__name__}: {e}")
            steps = [{**s, "status": "failed"} if s["status"] in ("waiting", "running") else s for s in steps]
        finally:
            db.end_run(conn, run_id, steps)


def _step(conn: sqlite3.Connection, run_id: int, steps: list[dict], cmd: list[str], log: IO[str], timeout: float,
          stoppable: bool) -> str:  # fmt: skip
    """Run one step's clipit, its output into the log, and return its status."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", RUN: str(run_id)}  # emoji; lines as logged
    if stoppable:
        env[WATCH] = str(os.getpid())
    kw: dict = {} if sys.platform == "win32" else {"start_new_session": True}  # its own group, to kill it whole
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env, **kw)
    deadline = time.monotonic() + timeout
    told = False
    while True:
        try:
            code = proc.wait(BEAT)
            return "done" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            pass
        stop = db.beat_run(conn, run_id, steps)
        if time.monotonic() > deadline:
            _say(log, f"killed: still running after {timeout / 3600:g} h")
            _kill(proc)
            return "failed"
        if stop and stoppable:
            _say(log, "stopped from the dashboard")
            _kill(proc)
            return "stopped"
        if stop and not told:
            _say(log, "stop asked: letting the YouTube post finish first, so it's never cut off half sent")
            told = True


_report: tuple[sqlite3.Connection, int] | None = None  # set in a step of a dashboard run (reporting)


def reporting(conn: sqlite3.Connection) -> None:
    """In a step of a dashboard run (its RUN is set): progress() reports to the run, through conn."""
    global _report
    if run_id := os.environ.get(RUN):
        _report = conn, int(run_id)


def progress(**doing: object) -> None:
    """What this step is doing now, shown in the Run tab: what (headline), vod ([n, of]), stage, count ([done, of]).
    Merged into what it said before, a None removes a field. Outside a dashboard run it does nothing, and a failed
    write is only logged: progress never fails the work. Call it from the thread that made the connection."""
    if _report is None:
        return
    try:
        db.run_doing(*_report, doing)
    except sqlite3.Error:
        logger.warning("couldn't report progress to the run", exc_info=True)


def _say(log: IO[str], line: str) -> None:
    log.write(f"[run] {line}\n")
    log.flush()


def _kill(proc: subprocess.Popen) -> None:
    """Kill the step and everything it started: ffmpeg, yt-dlp, the AI CLI."""
    kill_tree(proc.pid)
    proc.kill()  # at least the step itself, if the tree kill failed
    proc.wait(KILL_WAIT)


def kill_tree(pid: int) -> None:
    """Kill the process and every process it started (on POSIX: its process group, as steps lead their own)."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=KILL_WAIT)
        else:
            os.killpg(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        logger.warning("couldn't kill the processes of %d", pid, exc_info=True)


def watch_runner(runner: int) -> None:
    """In a clipping step: when the runner that supervises it dies (killed, crashed), end this step and everything
    it started, so clipping never goes on unseen, without its watchdog and out of Stop's reach."""
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32
        # Not the parent pid: a venv's python.exe is a launcher that starts the real interpreter as its child. The
        # runner is alive when it starts us, and a handle keeps its process object, so its pid can't be reused
        # while we wait on it. 0 (it's gone already) counts as gone.
        handle = kernel.OpenProcess(0x00100000, False, runner)  # SYNCHRONIZE

        def gone() -> None:
            if handle:
                kernel.WaitForSingleObject(handle, 0xFFFFFFFF)  # until it exits
    else:

        def gone() -> None:
            while os.getppid() == runner:  # a dead parent's children are handed to init (or a subreaper)
                time.sleep(BEAT)

    def wait() -> None:
        gone()
        logger.error("the run's runner is gone: stopping this clipping step")
        kill_tree(os.getpid())
        os._exit(1)  # if the tree kill failed

    threading.Thread(target=wait, daemon=True).start()
