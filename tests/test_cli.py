import datetime

import pytest

from clipit import cli
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
