import sys
from pathlib import Path

import pytest

from clipit import cli, poll, youtube


class Stop(Exception):
    pass


def test_every_repeats_passes_and_survives_a_failed_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    passes: list[str] = []
    sleeps: list[float] = []

    def fake_poll(*args: object, **kwargs: object) -> None:
        passes.append("poll")
        if len(passes) == 1:
            raise RuntimeError("twitch is down")

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    monkeypatch.setattr(sys, "argv", ["clipit", "--poll", "--upload", "--every", "45", "--out", str(tmp_path)])
    monkeypatch.setattr(cli, "setup_logging", lambda out: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: name)
    monkeypatch.setattr(poll, "poll", fake_poll)
    monkeypatch.setattr(youtube, "upload_next", lambda conn, out: None)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        cli.main()
    assert passes == ["poll"] * 3  # the failed first pass didn't end the loop
    assert sleeps == [2700] * 3


@pytest.mark.parametrize("args", [["--every", "30"], ["--dashboard", "--every", "30"], ["--poll", "--every", "0"]])
def test_every_rejects_what_it_cannot_repeat(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["clipit", *args])
    with pytest.raises(SystemExit):
        cli.main()
