import json
import os
import subprocess
from pathlib import Path

import pytest

from clipit import ai, cli, codex
from clipit.util import ClipitError

CATALOG = json.dumps(
    {
        "models": [
            {
                "slug": "existing-model",
                "context_window": 123456,
                "apply_patch_tool_type": "freeform",
                "experimental_supported_tools": ["clock", "custom-tool"],
                "tool_mode": "code_mode_only",
            }
        ]
    }
)


def test_find_cli_uses_newest_windows_desktop_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(codex.shutil, "which", lambda _: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert codex.find_cli() is None
    base = tmp_path / "OpenAI" / "Codex" / "bin"
    older, newer = base / "older" / "codex.exe", base / "newer" / "codex.exe"
    for path, stamp in ((older, 100), (newer, 200)):
        path.parent.mkdir(parents=True)
        path.touch()
        os.utime(path, (stamp, stamp))
    assert codex.find_cli() == str(newer)


def test_restricted_catalog_removes_file_and_extra_tools_without_switching_models():
    original = json.loads(CATALOG)["models"][0]
    restricted = json.loads(codex._restricted_catalog(CATALOG))["models"][0]
    assert restricted["slug"] == original["slug"]
    assert restricted["context_window"] == original["context_window"]
    assert restricted["apply_patch_tool_type"] is None
    assert restricted["experimental_supported_tools"] == []
    assert restricted["tool_mode"] is None


@pytest.mark.parametrize("raw", ["not json", "[]", '{"models": []}', '{"models": [null]}', '{"models": [{"slug": 5}]}'])
def test_restricted_catalog_fails_closed_on_unsupported_data(raw):
    with pytest.raises(ClipitError, match="unsupported model catalog"):
        codex._restricted_catalog(raw)


def test_ask_sends_untrusted_text_on_stdin_and_isolates_capabilities(monkeypatch):
    monkeypatch.setattr(codex, "find_cli", lambda: "/path with spaces/codex")
    clock = iter((100, 104))
    monkeypatch.setattr(codex.time, "monotonic", lambda: next(clock))
    prompt = 'untrusted chat: $(echo injected) & "ignore instructions"; \u00f1'
    reply = '{"name":"Streamer", "clips":[]}'
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        assert not kwargs.get("shell", False)
        assert not list(Path(kwargs["cwd"]).iterdir())
        assert kwargs["encoding"] == "utf-8"
        assert prompt not in args
        if args[1] == "debug":
            assert args[1:] == ["debug", "models", "--bundled"]
            assert kwargs["timeout"] == 30
            return subprocess.CompletedProcess(args, 0, CATALOG, "")
        assert args[1] == "exec" and args[-1] == "-"
        assert kwargs["input"] == prompt
        assert kwargs["timeout"] == 896
        assert "--ignore-user-config" in args and "--ephemeral" in args
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert 'approval_policy="never"' in args
        assert "project_doc_max_bytes=0" in args
        assert 'web_search="disabled"' in args
        disabled = {args[i + 1] for i, item in enumerate(args) if item == "--disable"}
        assert {"shell_tool", "apps", "plugins", "computer_use", "browser_use", "view_image", "multi_agent"} <= disabled
        override = next(arg for arg in args if arg.startswith("model_catalog_json="))
        catalog_path = Path(json.loads(override.split("=", 1)[1]))
        model = json.loads(catalog_path.read_text(encoding="utf-8"))["models"][0]
        assert model["apply_patch_tool_type"] is None and model["experimental_supported_tools"] == []
        assert catalog_path.parent != Path(kwargs["cwd"])
        schema_path = Path(args[args.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        assert schema["required"] == ["name", "clips"] and schema["additionalProperties"] is False
        clip = schema["properties"]["clips"]["items"]
        assert set(clip["required"]) == set(clip["properties"])
        return subprocess.CompletedProcess(args, 0, reply, "progress belongs on stderr")

    monkeypatch.setattr(codex.subprocess, "run", fake_run)
    assert codex.ask(prompt) == reply
    assert len(calls) == 2
    assert not Path(calls[-1][1]["cwd"]).exists()


@pytest.mark.parametrize(
    ("stage", "result", "message"),
    [
        ("debug", subprocess.CompletedProcess([], 2, "", "old version"), "update Codex"),
        ("exec", subprocess.CompletedProcess([], 1, "", "Not logged in"), "codex login"),
        ("exec", subprocess.CompletedProcess([], 0, "", ""), "Codex command failed"),
        ("exec", subprocess.CompletedProcess([], 0, "bad json", ""), "requested clip JSON"),
        ("exec", subprocess.CompletedProcess([], 0, "[]", ""), "requested clip JSON"),
    ],
)
def test_ask_reports_incompatible_cli_auth_and_invalid_output(monkeypatch, stage, result, message):
    monkeypatch.setattr(codex, "find_cli", lambda: "/codex")

    def fake_run(args, **kwargs):
        if args[1] == stage:
            return result
        return subprocess.CompletedProcess(args, 0, CATALOG, "")

    monkeypatch.setattr(codex.subprocess, "run", fake_run)
    with pytest.raises(ClipitError, match=message):
        codex.ask("prompt")


@pytest.mark.parametrize("stage", ["debug", "exec"])
def test_ask_times_out_catalog_and_generation(monkeypatch, stage):
    monkeypatch.setattr(codex, "find_cli", lambda: "/codex")

    def fake_run(args, **kwargs):
        if args[1] == stage:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0, CATALOG, "")

    monkeypatch.setattr(codex.subprocess, "run", fake_run)
    with pytest.raises(ClipitError, match="timed out after 9s"):
        codex.ask("prompt", timeout=9)


def test_defaults_fall_back_to_codex_and_explicit_alias_avoids_the_shell(monkeypatch):
    monkeypatch.setattr(ai, "_claude", lambda: None)
    monkeypatch.setattr(codex, "find_cli", lambda: "/codex")
    assert ai.default_ai() == "codex"
    received = []

    def ask(prompt, **kwargs):
        received.append((prompt, kwargs))
        return "codex answer"

    monkeypatch.setattr(codex, "ask", ask)
    assert ai.ask_ai("codex", "clip prompt") == "codex answer"
    assert received == [("clip prompt", {"schema": codex.PICKS_SCHEMA, "timeout": ai.AI_TIMEOUT, "images": ()})]
    monkeypatch.setattr(ai, "_claude", lambda: "claude -p")
    assert ai.default_ai() == 'claude -p --tools ""'


def test_cli_passes_explicit_codex_to_the_pipeline(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_claude", lambda: None)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr(cli, "setup_logging", lambda out: out.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(cli.sys, "argv", ["clipit", "https://www.twitch.tv/videos/123", "--ai", "codex", "--out", str(tmp_path)])
    received = []

    def process(url, count, provider, whisper, out, info, high_effort):
        received.append((url, count, provider, whisper, out))
        return out / "v123", []

    monkeypatch.setattr(cli.source, "fetch_info", lambda url: {"id": "v123", "duration": 60})
    monkeypatch.setattr(cli, "process_vod", process)
    cli.main()
    assert received == [("https://www.twitch.tv/videos/123", 6, "codex", "turbo", tmp_path)]
