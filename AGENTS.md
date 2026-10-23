# AGENTS.md

Instructions for coding agents working on clip-it. Read this before changing anything.

clip-it turns hours-long Twitch VODs into vertical TikTok / YouTube Shorts clips. It runs **unattended** on a
timer (polling Twitch), so treat every change as production code: it has to survive crashes, hangs, hostile
input, and running for weeks without anyone watching.

## Commands

```bash
python -m pip install -c requirements.lock -e ".[dev]"   # install, versions pinned by the lockfile
pre-commit install                                       # once per clone: lint + format on every commit
ruff check .                                             # lint, must pass
ruff format .                                            # format (CI runs `ruff format --check .`)
pyright                                                  # type check, must pass
pytest -q                                                # tests, must pass
clipit --poll --dry-run                                  # see what polling would clip, touches nothing
```

A change is not done until `ruff check .`, `ruff format --check .`, `pyright` and `pytest -q` all pass. CI runs all four
on every push and pull request. Changes to media handling (ffmpeg, Whisper,
facecam, captions) also need a check on a real VOD: render a clip and look at a frame from it.

## Architecture

`clipit/` has one module per pipeline stage. `pipeline.process_vod()` runs them in order; `poll.py` decides which
VODs to process; `cli.py` is the only place that parses flags, sets up logging, or exits the process.

| Data | Where it lives |
|---|---|
| State shared across runs (which VODs are done, clips, later uploads and accounts) | SQLite, `out/clipit.db`, only through `clipit/db.py` |
| Per-VOD working files (audio, chat, transcripts, AI reply, renders) | `out/<vod-id>/`, a cache that is safe to delete |
| Logs | `out/logs/clipit.log` (rotating) |

## Rules

### State
- **Never add a JSON file (or any other ad-hoc file) for state shared across runs.** Add a table or column in `db.py`.
  An earlier version used `poll_state.json`; it was replaced because a crash mid-write corrupts it and two runs
  overwrite each other.
- Schema changes are **append-only migrations**: add a new entry to `db.MIGRATIONS`, never edit a shipped one.
- Anything that picks work to do must **claim it atomically** (`db.claim`, `BEGIN IMMEDIATE`), so two overlapping
  runs can never process the same item.
- Cache files under `out/<vod-id>/` are written with `util.write_atomic`, never a plain `write_text`, so a crash
  can't leave a half-written file that the next run trusts.

### External calls
- **Every subprocess, network call and AI call has a timeout.** A hang must turn into an error the poller can record,
  never a stuck process. `util.sh` requires one. For streamed reads (`Popen`), use a watchdog timer that kills the process.
- ffmpeg inputs that are URLs get `util.REMOTE` (the protocol whitelist).
- The Twitch GQL API is unofficial. Expect it to change, and fail loudly (`ClipitError`, logged warnings), never silently.

### Untrusted input
Stream titles, chat, transcripts, VOD metadata, web pages and **everything the AI returns** come from strangers.
- AI prompts: untrusted text goes inside the `<stream_data>` fence (see `ai.build_prompt`) and must not be able to
  close it. The clip-picking AI runs **with no tools** and in an empty temp folder. Only trend research gets web
  tools, and its prompt contains no untrusted text. Never give the picking command tools.
- AI output is validated before use (`ai.clean`): numbers parsed and clamped, text fields forced to capped strings.
- Untrusted text never reaches a shell. File names go through `util.slug`, folder names are checked against a strict
  pattern, subtitle text goes through `render.make_ass`'s escaping (strips `\ { }` and newlines).

### Errors and logging
- Library code raises `ClipitError` for expected failures. Only `cli.py` calls `sys.exit`.
- One bad VOD must never stop a polling pass: `poll.py` catches it, logs it with its traceback, records it in the database.
- Log with `util.log` / `util.logger`, never `print`. Use `logger.exception` when a traceback helps.

### Code
- Python 3.11+, type hints on every public function (pyright checks them), imports sorted by ruff.
- Silence pyright only for wrong third-party stubs, with `# pyright: ignore[rule]` and the reason on the same line.
- New logic gets a pytest test in `tests/`, using fakes (`monkeypatch`, `tmp_path`) instead of the network.
- New dependency: add it to `pyproject.toml`, then regenerate the lockfile
  (`pip freeze --exclude-editable > requirements.lock`) and say why in the commit.
- Never commit `out/`, `.venv/`, credentials, browser profiles or tokens.
- Branch names are `<type>/<short-name>` in lowercase (CI rejects others): `feat/` new feature, `fix/` bug fix,
  `refactor/` no behavior change, `test/` tests only. Example: `fix/stale-claim`.

## Mistakes made before (don't repeat them)
- Stored shared state in a JSON file instead of a database.
- Shipped without timeouts, so one hung AI or ffmpeg call could freeze the poller forever.
- Gave the clip-picking AI web tools while its prompt contained Twitch chat, which opened a prompt-injection path to
  reading local files and sending them out.
- Put a `Popen` timeout after a blocking read loop, where it could never fire.
- Wrote a test whose setup painted over the very thing it was testing (always check that a test can fail).
- Trusted a single heuristic on a few frames (facecam detection) instead of checking it against real data.
