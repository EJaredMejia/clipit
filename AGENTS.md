# AGENTS.md

clip-it turns hours-long Twitch and Kick VODs into vertical TikTok / YouTube Shorts clips. It runs **unattended** on a
timer, so every change has to survive crashes, hangs, hostile input, and weeks of running with nobody watching.

## Commands

Run each from the repo root:

```bash
python -m pip install -c requirements.lock -e ".[dev]"
ruff check .
ruff format --check .
pyright
pytest -q
npm --prefix web ci
npm --prefix web test
npm --prefix web run build
npm --prefix site ci
npm --prefix site run build
npm --prefix site run typecheck
```

A change is done when these pass (in a worktree, run pyright with `--pythonpath` set to the venv's python). `web/` is
the dashboard page; `npm run build` writes it to `clipit/web/`, which is build output: never edit it by hand, and commit
the rebuilt files (CI fails if they don't match `web/src/`). `site/` is the public site, deployed by CI from main only.
Media changes (ffmpeg, Whisper, facecam, captions): render a clip from a real VOD and look at a frame. Page changes: look
at it in a browser.

## Architecture

Always build code cleanly, modularly, and with proper architecture and separation of concerns. Never simply bolt on a
feature because I asked for it. If you can't find an easy way to build it naturally, and you'd recommend a refactor to
make the whole thing cleanly, stop your work ahead of time and say so.

If a rule in this file about how code is structured gets in the way of clean code, delete it in the same change and
say why. Never delete a product decision (like the dashboard rule below) or anything under Rules on your own: if one
gets in the way, stop and ask.

- Library code never parses flags, sets up logging, or exits the process; only the entry point does.
- Python is the only server. The page in `web/` talks only to `dashboard.py`'s API, never to SQLite directly.
- Every feature gets a dashboard control, not only a CLI flag.

## Rules

- **State:** anything shared across runs lives in SQLite through `db.py`, never a JSON or other ad-hoc file.
  Migrations are append-only. Anything that picks work claims it atomically (`db.claim`). Cache files under
  `out/<vod-id>/` are written with `util.write_atomic`.
- **External calls:** every subprocess, network call and AI call has a timeout. A hang must become an error, never a
  stuck process. ffmpeg inputs that are URLs get `util.REMOTE` (the protocol whitelist).
- **Untrusted input:** stream titles, chat, transcripts, VOD metadata, web pages and everything the AI returns.
  - It goes inside the prompt's `<stream_data>` fence and never reaches a shell. AI output is validated in code, and
    anything a prompt asks for that matters is also checked in code.
  - The clip-picking AI gets no tools that touch files, commands or the network. The Codex provider also ignores user
    config, runs read-only, and its temporary model catalog drops `apply_patch_tool_type` and model-specific tools.
  - Slurs and explicit words are starred out of any text we post or burn in (`util.filter_words`).
  - The page shows it as React text only. The dashboard server keeps its Host and Origin checks, JSON-only writes and
    CSP; never loosen them, not even for the dev server.
- **Posting:** only through a platform's official API. Never download or repost someone else's clip.
  - Automatic posts take only clips from the last `db.FRESH`: Keep first, then unreviewed ones scored `db.AUTO_SCORE`
    or more that pass `pipeline.review`, never Skip, at most `youtube.PER_DAY`. A run with no good clip means polling
    chose badly: fix that, never lower the bar.
  - Every upload is claimed (`db.claim_upload`) before it's sent, and one that broke off is never retried automatically:
    the video may already be up.
- **Errors:** library code raises `ClipitError`; one bad VOD never stops a polling pass. Log with `util.log`, never
  `print`. Prefer failing a VOD so it's retried over quietly producing worse output.
- **Tests:** new logic gets a pytest test with fakes, and the test must be able to fail on a real bug.
- **Dependencies:** add to `pyproject.toml` and regenerate `requirements.lock`
  (`pip freeze --exclude-editable > requirements.lock`); for the page, `npm install` in `web/`. Say why in the commit.
- Never commit `out/`, `.venv/`, `node_modules/`, credentials or tokens. Branches are `<type>/<short-name>`
  (`feat/`, `fix/`, `refactor/`, `test/`).
