import datetime
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from clipit import db, runs
from clipit.util import utcnow

NAMES = [name for name, _, _ in runs.steps(runs.SETTINGS)]


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(runs, "BEAT", 0.1)
    c = db.connect(tmp_path / "clipit.db")
    yield c
    c.close()


def fake(monkeypatch: pytest.MonkeyPatch, scripts: dict[str, str]) -> None:
    monkeypatch.setattr(runs, "command", lambda args, out: [sys.executable, "-c", scripts[args[0]], *args])


def in_background(conn: sqlite3.Connection, out: Path) -> tuple[int, threading.Thread]:
    run_id = db.start_run(conn, NAMES)
    assert run_id
    path = conn.execute("PRAGMA database_list").fetchone()["file"]

    def go() -> None:  # the runner has its own connection, like its own process
        own = db.connect(Path(path))
        try:
            runs.run(own, out, run_id)
        finally:
            own.close()

    t = threading.Thread(target=go)
    t.start()
    return run_id, t


def wait_for(check, what: str) -> None:
    for _ in range(200):
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(f"never: {what}")


def statuses(conn: sqlite3.Connection) -> list[str]:
    run = db.latest_run(conn)
    assert run
    return [s["status"] for s in run["steps"]]


def test_every_step_runs_even_after_one_fails_and_its_output_is_kept(conn, tmp_path, monkeypatch) -> None:
    fake(monkeypatch, {"--poll": "print('clipping caedrel ✓'); raise SystemExit(1)", "--upload": "print('posted')"})
    run_id = db.start_run(conn, NAMES)
    assert run_id
    runs.run(conn, tmp_path, run_id)
    run = db.latest_run(conn)
    assert run and not run["running"] and run["ended"]
    assert statuses(conn) == ["failed", "done"]
    lines = runs.tail(tmp_path, run_id)
    assert "clipping caedrel ✓" in lines and "posted" in lines


def test_stop_kills_a_clipping_step_with_everything_it_started_and_skips_the_rest(conn, tmp_path, monkeypatch) -> None:
    ticks = tmp_path / "ticks"
    # the step starts a child that keeps writing, like ffmpeg under clipit --poll
    child = f"import time\nwhile True:\n    open({str(ticks)!r}, 'a').write('x'); time.sleep(0.05)"
    poll = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)"
    fake(monkeypatch, {"--poll": poll, "--upload": "print('posted')"})
    run_id, t = in_background(conn, tmp_path)
    wait_for(lambda: ticks.exists(), "the child started")
    assert db.stop_run(conn)
    t.join(30)
    assert statuses(conn) == ["stopped", "stopped"]
    size = ticks.stat().st_size
    time.sleep(0.5)
    assert ticks.stat().st_size == size  # the child is gone too
    assert "posted" not in runs.tail(tmp_path, run_id)


def test_stop_lets_a_youtube_post_finish(conn, tmp_path, monkeypatch) -> None:
    started = tmp_path / "started"
    upload = f"import time; open({str(started)!r}, 'w').close(); time.sleep(1); print('posted')"
    fake(monkeypatch, {"--poll": "pass", "--upload": upload})
    run_id, t = in_background(conn, tmp_path)
    wait_for(started.exists, "the upload started")
    assert db.stop_run(conn)
    assert db.latest_run(conn)["stopping"]  # type: ignore[index]
    t.join(30)
    assert statuses(conn) == ["done", "done"]
    assert "posted" in runs.tail(tmp_path, run_id)


def test_a_hung_step_is_killed(conn, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runs, "POLL_TIMEOUT", 0.5)
    fake(monkeypatch, {"--poll": "import time; time.sleep(60)", "--upload": "print('posted')"})
    run_id = db.start_run(conn, ["Clip", "Post"])
    assert run_id
    runs.run(conn, tmp_path, run_id)
    assert statuses(conn) == ["failed", "done"]


def test_one_run_at_a_time_and_a_dead_runner_shows_as_interrupted(conn) -> None:
    now = utcnow()
    assert db.start_run(conn, NAMES, now=now)
    assert db.start_run(conn, NAMES, now=now) is None
    later = now + db.RUN_DEAD + datetime.timedelta(seconds=1)  # the runner stopped beating: PC restart, killed
    run = db.latest_run(conn, later)
    assert run and not run["running"] and run["ended"] == now.isoformat()
    assert [s["status"] for s in run["steps"]] == ["interrupted", "interrupted"]
    assert db.start_run(conn, NAMES, now=later)


def test_tail_reads_only_the_end_of_a_long_log(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runs, "LOG_TAIL", 100)
    (tmp_path / "logs").mkdir()
    runs.log_file(tmp_path, 1).write_text("".join(f"line {i}\n" for i in range(1000)), encoding="utf-8")
    assert runs.tail(tmp_path, 1, lines=3) == ["line 997", "line 998", "line 999"]
    assert runs.tail(tmp_path, 2) == []


def test_a_clipping_step_ends_with_everything_it_started_when_its_runner_dies(tmp_path) -> None:
    ticks, step_pid = tmp_path / "ticks", tmp_path / "step.pid"
    child = f"import time\nwhile True:\n    open({str(ticks)!r}, 'a').write('x'); time.sleep(0.05)"  # like ffmpeg
    step = (
        "import os, subprocess, sys, time\nfrom clipit import runs\nruns.watch_runner(int(os.environ[runs.WATCH]))\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(60)"
    )
    # a runner that starts the step as runs._step does, then is killed alone: its step is left without it
    runner = (
        "import os, subprocess, sys, time\nfrom clipit import runs\n"
        "kw = {} if sys.platform == 'win32' else {'start_new_session': True}\n"
        f"p = subprocess.Popen([sys.executable, '-c', {step!r}], env={{**os.environ, runs.WATCH: str(os.getpid())}}, **kw)\n"
        f"open({str(step_pid)!r}, 'w').write(str(p.pid))\ntime.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", runner])
    try:
        wait_for(ticks.exists, "the step's child started")
        time.sleep(runs.BEAT + 1)
        size = ticks.stat().st_size
        time.sleep(0.5)
        assert ticks.stat().st_size > size  # still clipping while its runner lives
        proc.kill()  # only the runner
        proc.wait(10)
        time.sleep(runs.BEAT + 2)  # the step notices (POSIX checks every BEAT) and kills its tree
        size = ticks.stat().st_size
        time.sleep(0.5)
        assert ticks.stat().st_size == size  # the step's child is gone too
    finally:
        if proc.poll() is None:
            proc.kill()
        if step_pid.exists():  # if the watch failed: don't leave it ticking
            runs.kill_tree(int(step_pid.read_text()))


def test_a_step_says_what_its_doing_and_the_next_step_starts_clean(conn, tmp_path, monkeypatch) -> None:
    go = tmp_path / "go"
    # a step reports as clipit's steps do (cli: runs.reporting), then waits so the test can look
    poll = (
        "import os, time\nfrom pathlib import Path\nfrom clipit import db, runs\n"
        f"runs.reporting(db.connect(Path({str(tmp_path / 'clipit.db')!r})))\n"
        "runs.progress(what='Clipping caedrel', vod=[2, 6], stage='Transcribing', count=[0, 12])\n"
        "runs.progress(count=[3, 12])\n"
        f"while not os.path.exists({str(go)!r}): time.sleep(0.05)"
    )
    upload = "import time; time.sleep(0.5)"
    fake(monkeypatch, {"--poll": poll, "--upload": upload})
    _, t = in_background(conn, tmp_path)
    want = {"what": "Clipping caedrel", "vod": [2, 6], "stage": "Transcribing", "count": [3, 12]}
    wait_for(lambda: db.latest_run(conn)["doing"] == want, "the step's progress shows")  # type: ignore[index]
    go.touch()
    wait_for(lambda: db.latest_run(conn)["doing"] == {"what": NAMES[1]}, "the next step starts clean")  # type: ignore[index]
    t.join(30)
    assert db.latest_run(conn)["doing"] == {}  # type: ignore[index]


def test_a_run_does_what_its_settings_say(conn, tmp_path, monkeypatch) -> None:
    fake(monkeypatch, {"--poll": "import sys; print(sys.argv[1:])", "--upload": "print('posted')"})
    job = {**runs.SETTINGS, "high_effort": True, "upload": False, "lang": "", "prioritize": ["twitch:caedrel"]}
    run_id = db.start_run(conn, [name for name, _, _ in runs.steps(job)], job)
    assert run_id
    runs.run(conn, tmp_path, run_id)
    assert statuses(conn) == ["done"]  # posting turned off: no YouTube step
    out = " ".join(runs.tail(tmp_path, run_id))
    assert "'--high-effort'" in out and "'--lang', ''" in out and "'--prioritize', 'twitch:caedrel'" in out


def test_run_settings_are_checked_before_they_become_flags(conn) -> None:
    ok = {**runs.SETTINGS, "lang": "es", "prioritize": ["Twitch:Caedrel"]}
    assert runs.clean_settings(ok) == {**ok, "lang": "ES", "prioritize": ["twitch:caedrel"]}
    for bad in ({"top": True}, {"clips": 0}, {"lang": "en --dry-run"}, {"prioritize": ["caedrel"]}, {"extra": 1}):
        assert runs.clean_settings({**runs.SETTINGS, **bad}) is None, bad
    assert runs.settings(conn) == runs.SETTINGS
    db.save_setting(conn, "run", {"top": 3, "gone": 1})  # saved by an older version: missing keys take the defaults
    assert runs.settings(conn) == {**runs.SETTINGS, "top": 3}


def test_a_reedit_job_passes_the_request_as_one_argument() -> None:
    job = runs.reedit_job(7, "  -x a boom here  ", 3)
    assert job == {"reedit": 7, "request": "-x a boom here", "at": 3}
    assert job
    [(_, args, _)] = runs.steps(job)
    assert args == ["--reedit", "7", "--request=-x a boom here", "--at", "3.00"]
    for bad in ((7, " ", None), (7, "x" * 501, None), (7, "x", float("nan")), (7, "x", True), (True, "x", None)):
        assert runs.reedit_job(*bad) is None, bad


def test_a_killed_reedit_still_shows_on_its_clip(conn, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runs, "REEDIT_TIMEOUT", 0.5)
    fake(monkeypatch, {"--reedit": "import time; time.sleep(60)"})
    db.record_manual(conn, "v1", "u")
    db.save_clips(conn, "v1", [{"idx": 1, "file": "v1/a.mp4", "start": 1.0, "end": 20.0, "title": "t", "hook": "",
                                "caption": "", "youtube": "", "streamer": "", "trend": "", "sound": "", "score": 7.0, "why": ""}])  # fmt: skip
    db.start_reedit(conn, 1, "a boom", None)  # what clipit --reedit does first, before it's killed
    job = runs.reedit_job(1, "a boom", None)
    assert job
    run_id = db.start_run(conn, ["Re-edit the clip"], job)
    assert run_id
    runs.run(conn, tmp_path, run_id)
    assert conn.execute("SELECT error FROM reedits").fetchone()[0] == "the run's step failed"
