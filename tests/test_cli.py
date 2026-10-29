import datetime
import sys
from pathlib import Path

import pytest

from clipit import cli, poll, youtube
from clipit.util import ClipitError


@pytest.fixture
def research(monkeypatch):
    """Fake trend research: records each call, fails when told to."""
    calls, state = [], {"age": None, "fail": False}

    def refresh(cmd):
        calls.append(cmd)
        if state["fail"]:
            raise ClipitError("web search down")

    monkeypatch.setattr(cli.ai, "trends_age", lambda: state["age"])
    monkeypatch.setattr(cli.ai, "refresh_trends", refresh)
    return calls, state


def test_missing_or_stale_trends_are_researched_fresh_ones_are_not(research):
    calls, state = research
    cli.refresh_stale_trends("codex")  # never researched
    state["age"] = cli.ai.TRENDS_MAX_AGE + datetime.timedelta(minutes=1)
    cli.refresh_stale_trends("codex")
    state["age"] = cli.ai.TRENDS_MAX_AGE - datetime.timedelta(minutes=1)
    cli.refresh_stale_trends("codex")
    cli.refresh_stale_trends(None)  # no AI for research installed
    assert calls == ["codex", "codex"]


def test_failed_research_does_not_stop_polling(research):
    calls, state = research
    state["fail"] = True
    cli.refresh_stale_trends("codex")  # logged, not raised
    assert calls == ["codex"]


class Stop(Exception):
    pass


def test_every_repeats_passes_and_survives_a_failed_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    passes: list[str] = []
    sleeps: list[float] = []

    def fake_poll(*args: object, **kwargs: object) -> None:
        passes.append("poll")
        if passes.count("poll") == 1:
            raise RuntimeError("twitch is down")

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    monkeypatch.setattr(sys, "argv", ["clipit", "--poll", "--upload", "--every", "45", "--out", str(tmp_path)])
    monkeypatch.setattr(cli, "setup_logging", lambda out: None)
    monkeypatch.setattr(cli, "refresh_stale_trends", lambda trends_ai: passes.append("trends"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: name)
    monkeypatch.setattr(poll, "poll", fake_poll)
    monkeypatch.setattr(youtube, "upload_next", lambda *a, **kw: None)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        cli.main()
    assert passes == ["trends", "poll"] * 3  # the failed first pass didn't end the loop; stale trends checked each pass
    assert sleeps == [2700] * 3


def test_prioritize_reaches_poll_and_upload_as_clean_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    got: dict[str, object] = {}
    argv = ["clipit", "--poll", "--upload", "--prioritize", " Twitch:Caedrel ", "--prioritize", "`kick:xqc`"]
    monkeypatch.setattr(sys, "argv", [*argv, "--out", str(tmp_path)])
    monkeypatch.setattr(cli, "setup_logging", lambda out: None)
    monkeypatch.setattr(cli, "refresh_stale_trends", lambda trends_ai: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: name)
    monkeypatch.setattr(poll, "poll", lambda *a, prioritize, **kw: got.update(poll=prioritize))
    monkeypatch.setattr(youtube, "upload_next", lambda conn, out, first, since: got.update(upload=first))
    cli.main()
    assert got == {"poll": [("twitch", "caedrel"), ("kick", "xqc")], "upload": ["twitch:caedrel", "kick:xqc"]}


def test_a_failed_upload_still_polls_then_exits_with_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    polled: list[bool] = []

    def no_login(*a: object, **kw: object) -> None:
        raise ClipitError("Not logged in to YouTube")

    monkeypatch.setattr(sys, "argv", ["clipit", "--poll", "--upload", "--out", str(tmp_path)])
    monkeypatch.setattr(cli, "setup_logging", lambda out: None)
    monkeypatch.setattr(cli, "refresh_stale_trends", lambda trends_ai: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: name)
    monkeypatch.setattr(poll, "poll", lambda *a, **kw: polled.append(True))
    monkeypatch.setattr(youtube, "upload_next", no_login)
    with pytest.raises(SystemExit) as exit:
        cli.main()
    assert exit.value.code == 1  # a dashboard run shows the post as failed, not done
    assert polled == [True]


def test_failed_vods_still_post_then_exit_with_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    posted: list[bool] = []
    monkeypatch.setattr(sys, "argv", ["clipit", "--poll", "--upload", "--out", str(tmp_path)])
    monkeypatch.setattr(cli, "setup_logging", lambda out: None)
    monkeypatch.setattr(cli, "refresh_stale_trends", lambda trends_ai: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: name)
    monkeypatch.setattr(poll, "poll", lambda *a, **kw: 2)  # every VOD of the pass failed
    monkeypatch.setattr(youtube, "upload_next", lambda *a, **kw: posted.append(True))
    with pytest.raises(SystemExit) as exit:
        cli.main()
    assert exit.value.code == 1  # a dashboard run shows clipping as failed, not done
    assert posted == [True]


@pytest.mark.parametrize(
    "every",
    [["--every", "30"], ["--dashboard", "--every", "30"], *(["--poll", "--every", m] for m in ("0", "nan", "inf"))],
)
def test_every_rejects_what_it_cannot_repeat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, every: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["clipit", *every, "--out", str(tmp_path)])
    monkeypatch.setattr(cli, "setup_logging", lambda out: pytest.fail("got past argument checks"))
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2  # argparse's usage error
