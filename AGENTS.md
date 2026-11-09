# AGENTS.md

clip-it turns hours-long Twitch and Kick VODs into vertical TikTok / YouTube Shorts clips. It runs **unattended** on a
timer, so every change has to survive crashes, hangs, hostile input, and weeks of running with nobody watching.

## Commands

```bash
python -m pip install -c requirements.lock -e ".[dev]"   # install
ruff check . && ruff format --check . && pyright && pytest -q
cd web && npm ci && npm test && npm run build             # dashboard page, writes clipit/web/
cd site && npm ci && npm run build && npm run typecheck   # public site, deployed by CI only
```

A change is done when all of these pass (in a worktree, run pyright with `--pythonpath` set to the venv's python), with
the rebuilt `clipit/web/` committed for `web/` changes. Media changes (ffmpeg, Whisper, facecam, captions): render a
clip from a real VOD and look at a frame. Page changes: look at it in a browser.

## Architecture

Always build code cleanly, modularly, and with proper architecture and separation of concerns. Never simply bolt on a
feature because I asked for it. If you can't find an easy way to build it naturally, and you'd recommend a refactor to
make the whole thing cleanly, stop your work ahead of time and say so.

If a rule in this file gets in the way of clean code, delete it in the same change and say why. The exceptions are the
safety rules under State, Timeouts, Untrusted input and Posting: if one of those gets in the way, stop and ask first.

- Library code never parses flags, sets up logging, or exits the process; only the entry point does.
- Python is the only server. The page in `web/` talks only to `dashboard.py`'s API, never to SQLite directly.
- Every feature gets a dashboard control, not only a CLI flag.

## Rules

- **State:** anything shared across runs lives in SQLite through `db.py`, never a JSON or other ad-hoc file.
  Migrations are append-only. Anything that picks work claims it atomically (`db.claim`). Cache files under
  `out/<vod-id>/` are written with `util.write_atomic`.
- **Timeouts:** every subprocess, network call and AI call has one. A hang must become an error, never a stuck process.
- **Untrusted input:** stream titles, chat, transcripts, web pages and everything the AI returns. It goes inside the
  prompt's `<stream_data>` fence, never reaches a shell, and the page shows it as React text only. The clip-picking AI
  gets no tools that touch files, commands or the network. AI output is validated in code, and anything a prompt asks
  for that matters is also checked in code.
- **Posting:** only through a platform's official API. Never download or repost someone else's clip.
- **Errors:** library code raises `ClipitError`; one bad VOD never stops a polling pass. Log with `util.log`, never
  `print`. Prefer failing a VOD so it's retried over quietly producing worse output.
- **Tests:** new logic gets a pytest test with fakes, and the test must be able to fail on a real bug.
- Never commit `out/`, `.venv/`, `node_modules/`, credentials or tokens. Branches are `<type>/<short-name>`
  (`feat/`, `fix/`, `refactor/`, `test/`).
