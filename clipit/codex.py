"""Codex CLI provider, isolated from user tools and filesystem-editing capabilities."""

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from clipit.util import ClipitError

# Keep local I/O unavailable even when a model's catalog enables it by default.
DISABLED_FEATURES = (
    "shell_tool",
    "apps",
    "plugins",
    "multi_agent",
    "view_image",
    "computer_use",
    "browser_use",
    "in_app_browser",
    "image_generation",
    "skill_search",
    "hooks",
    "memories",
    "code_mode_host",
    "code_mode_only",
    "daemon_auto_start",
    "goals",
    "sleep_tool",
    "tool_suggest",
    "workspace_dependencies",
    "request_permissions_tool",
)
TEXT_FIELDS = ("title", "hook", "description", "trend", "sound", "why")
CLIP_PROPERTIES = {
    "start": {"type": "number"},
    "end": {"type": "number"},
    **{field: {"type": "string"} for field in TEXT_FIELDS},
    "hashtags": {"type": "array", "items": {"type": "string"}},
    "score": {"type": "number"},
}
PICKS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "clips"],
    "properties": {
        "name": {"type": "string"},
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(CLIP_PROPERTIES),
                "properties": CLIP_PROPERTIES,
            },
        },
    },
}


def _strict(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}


SPAN = {"start": {"type": "number"}, "end": {"type": "number"}}
EDIT_PROPERTIES = {
    "cuts": {"type": "array", "items": _strict(SPAN)},
    "zooms": {"type": "array", "items": _strict({**SPAN, "zoom": {"type": "number"}})},
    "sfx": {"type": "array", "items": _strict({"at": {"type": "number"}, "sound": {"type": "string"}})},
    "texts": {"type": "array", "items": _strict({**SPAN, "text": {"type": "string"}})},
    "emphasis": {"type": "array", "items": {"type": "string"}},
    "layout": {"type": "string", "enum": ["split", "cam", "crop", "fill"]},
    "focus": {"type": "number"},
}
# A review of one high-effort clip (clipit.edit): the verdict on the current version, then the improved version.
EDIT_SCHEMA = _strict(
    {
        "name": {"type": "string"},
        "edit_score": {"type": "number"},
        "request_done": {"type": "boolean"},
        "problems": {"type": "array", "items": {"type": "string"}},
        "clip": _strict({**CLIP_PROPERTIES, "edit": _strict(EDIT_PROPERTIES)}),
    }
)


def find_cli() -> str | None:
    """Find Codex on PATH, or in the Windows desktop app's versioned CLI bundles."""
    found = shutil.which("codex")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    candidates = list((Path(local) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
    return str(max(candidates, key=lambda p: p.stat().st_mtime)) if candidates else None


def _restricted_catalog(raw: str) -> str:
    """Retain model identities and limits, removing file edits and extra model-specific tools."""
    try:
        catalog = json.loads(raw)
        models = catalog.get("models") if isinstance(catalog, dict) else None
        if not isinstance(models, list) or not models:
            raise ValueError("empty model catalog")
        for model in models:
            if not isinstance(model, dict) or not isinstance(model.get("slug"), str):
                raise ValueError("invalid model entry")
            model["apply_patch_tool_type"] = None
            model["experimental_supported_tools"] = []
            model["tool_mode"] = None
        return json.dumps(catalog)
    except (ValueError, TypeError) as exc:
        raise ClipitError("Codex returned an unsupported model catalog; update the Codex CLI") from exc


def ask(prompt: str, *, schema: dict = PICKS_SCHEMA, timeout: float = 900, images: Sequence[Path] = ()) -> str:
    """Run a fresh Codex request with only the supplied prompt.
    The reply must match schema: the clip picks, or a high-effort review.
    images: pictures attached to the prompt (no tool reads them: view_image stays disabled)."""
    executable = find_cli()
    if not executable:
        raise ClipitError("Codex CLI not found. Install Codex, run `codex login`, then retry --ai codex")
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="clipit-codex-") as folder:
        root = Path(folder)
        empty = root / "work"
        empty.mkdir()
        try:
            catalog = subprocess.run(
                [executable, "debug", "models", "--bundled"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=empty,
                timeout=min(30, timeout),
            )
            if catalog.returncode:
                raise ClipitError("Codex CLI must support `debug models --bundled`; update Codex.\n" + catalog.stderr[-2000:])
            catalog_path = root / "models.json"
            catalog_path.write_text(_restricted_catalog(catalog.stdout), encoding="utf-8")
            args = [
                executable,
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "-c",
                'approval_policy="never"',
                "-c",
                "project_doc_max_bytes=0",
                "-c",
                'web_search="disabled"',
                "-c",
                "model_catalog_json=" + json.dumps(catalog_path.as_posix()),
            ]
            for feature in DISABLED_FEATURES:
                args += ["--disable", feature]
            schema_path = root / "picks.schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            args += ["--output-schema", str(schema_path)]
            for image in images:
                args += ["--image", str(Path(image).resolve())]
            args.append("-")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            result = subprocess.run(
                args,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=empty,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            raise ClipitError(f"Codex timed out after {timeout:g}s") from None
        except OSError as exc:
            raise ClipitError(f"Could not start Codex: {exc}") from exc
    reply = result.stdout.strip()
    if result.returncode or not reply:
        raise ClipitError(
            f"Codex command failed:\n{(result.stdout + result.stderr)[-2000:]}\n"
            "If not logged in, run `codex login` once and retry."
        )
    try:
        if not isinstance(json.loads(reply), dict):
            raise ValueError("not a JSON object")
    except ValueError as exc:
        raise ClipitError("Codex did not return the requested clip JSON") from exc
    return reply
