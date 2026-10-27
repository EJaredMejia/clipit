# AGENTS.md

Instructions for coding agents working on clip-it. Read this before changing anything.

clip-it turns hours-long Twitch VODs into vertical TikTok / YouTube Shorts clips. It runs **unattended** on a
timer (polling Twitch), so treat every change as production code: it has to survive crashes, hangs, hostile
input, and running for weeks without anyone watching.

## Commands

```bash
python -m pip install -c requirements.lock -e ".[dev]"   # install, versions pinned by the lockfile
uv pip install -c requirements.lock -e ".[dev]"          # same with uv (after `uv venv`); CI uses pip
pre-commit install                                       # once per clone: lint + format on every commit
ruff check .                                             # lint, must pass
ruff format .                                            # format (CI runs `ruff format --check .`)
pyright                                                  # type check, must pass
pytest -q                                                # tests, must pass
clipit --poll --dry-run                                  # see what polling would clip, touches nothing
```

The dashboard page is a React + TypeScript app in `web/` (Node 22.12+), built with Vite:

```bash
cd web && npm ci                                         # install, versions pinned by package-lock.json
npm run dev                                              # hot-reloading page on :5173, API proxied to a running clipit --dashboard
npm test                                                 # Vitest, must pass
npm run build                                            # type check, then write the page to clipit/web/
```

A change is not done until `ruff check .`, `ruff format --check .`, `pyright` and `pytest -q` all pass, and for `web/`
changes `npm test` and `npm run build` too, with the rebuilt `clipit/web/` committed. CI runs all of them on every push
and pull request, and fails if `clipit/web/` doesn't match `web/src/`. Changes to media handling (ffmpeg, Whisper,
facecam, captions) also need a check on a real VOD: render a clip and look at a frame from it. Changes to the page
also need a look in a browser.

## Architecture

`clipit/` has one module per pipeline stage. `pipeline.process_vod()` runs them in order; `poll.py` decides which
VODs to process; `cli.py` is the only place that parses flags, sets up logging, or exits the process.

| Module | Job |
|---|---|
| `cli.py` | command-line flags, then calls the pipeline |
| `pipeline.py` | `process_vod(url, ...)`: runs every stage with caching and returns `(folder, rendered clips)` |
| `db.py` | the SQLite catalog: schema migrations, claiming VODs, recording clips |
| `source.py` | yt-dlp: VOD info, audio download, stream URLs |
| `twitch.py` | Twitch's public API: chat replay, live streams, latest VODs |
| `kick.py` | the same for Kick, through the JSON API its website uses, throttled to stay under its rate limit |
| `poll.py` | picks which streamers to clip and runs the pipeline on each, never the same VOD twice |
| `moments.py` | loudness + chat scoring, picks candidate windows |
| `transcribe.py` | Whisper on the candidate windows |
| `ai.py` | prompts, calling the AI CLI, validating its picks, trend research |
| `codex.py` | isolated Codex CLI provider, restricted model catalog and structured clip output |
| `facecam.py` | webcam overlay detection |
| `render.py` | captions, ffmpeg render, `posts.md` |
| `util.py` | logging, shell helpers with timeouts, atomic writes, `ClipitError` |
| `dashboard.py` | `--dashboard`: local HTTP server (127.0.0.1) for the built page in `clipit/web/` and its JSON API, reads and writes via `db.py` |
| `youtube.py` | `--youtube-login` and `--upload`: posts kept clips to YouTube Shorts, records each post in `uploads` |

`web/src/` is the dashboard page: `store.ts` is its zustand store (state, and every call to the server as plain
functions), and components read it through selectors, `useClipit((s) => s.tab)`, so each re-renders only when what it
reads changes. Actions replace state with `set()`, never mutate it: a selector can't see an in-place change. Python
stays the only server: the page never talks to SQLite or anything else directly, only to `dashboard.py`'s API.

| Page file | Job |
|---|---|
| `App.tsx` | header, navigation (pill bar on wide screens, tab bar on phones), the clip side sheet, the connection light |
| `Review.tsx` | the deck: drag, throw, keys, undo |
| `Post.tsx` | kept clips waiting to go up, one at a time |
| `Library.tsx` | every clip as a frame: filters, search, opens the sheet |
| `Editor.tsx` | one clip's post text, upload steps, verdict and delete: used by Post and the sheet |
| `Vods.tsx` | the VODs table |
| `ui.tsx` | formatting, links, badges, lazy video frames, confirm dialogs |

The UI is built from shadcn/ui components on Base UI (not Radix) and Tailwind v4, in `web/src/components/ui/`, added
with `npx shadcn@latest add <name>` from `web/`; edit them only where the app needs it. Base UI uses a `render` prop
where Radix had `asChild`. Theme colors (the pink accent, keep, skip, warn) are CSS variables in `web/src/index.css`,
and light/dark follows the OS (`prefers-color-scheme`). The CSP allows only the page's own files, so no web fonts, CDN
scripts or `data:` images: use OS fonts and lucide icons.

Motion follows a few rules: keyboard actions never animate; UI motion stays under 300 ms with ease-out curves (never
ease-in); movement and appearing use only `transform` and `opacity`, never size or layout (colors may fade on hover);
anything you can drag follows the pointer 1:1 and is thrown with its release speed; `prefers-reduced-motion` gets
fades or nothing, never movement. Any player showing a clip's video lets go of it while the
clip is deleted (`released` in the store): Windows can't delete a file that's being read.

Planned: auto-upload to TikTok, and to several accounts per platform (accounts as a new table). TikTok posts are
recorded by hand from the dashboard until then.

| Data | Where it lives |
|---|---|
| State shared across runs (which VODs are done, clips, later uploads and accounts) | SQLite, `out/clipit.db`, only through `clipit/db.py` |
| Per-VOD working files (audio, chat, transcripts, AI reply, renders) | `out/<vod-id>/`, a cache that is safe to delete |
| Logs | `out/logs/clipit.log` (rotating) |
| YouTube OAuth client and login token | `~/.clipit/`, never in the repo or `out/` |

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
- The Twitch GQL API and Kick's web API are unofficial. Expect it to change, and fail loudly (`ClipitError`, logged warnings), never silently.

### Posting
- Post only through a platform's official API, never by automating its website: that's what gets channels banned.
- Automatic posting takes only clips the user marked Keep, a few a day (`youtube.PER_DAY`, `youtube.GAP`). The
  dashboard's "Post to YouTube now" is the user posting by hand, like uploading in YouTube Studio: any clip, no pacing,
  but it counts toward the pace of the automatic posts.
- Each upload is claimed in `uploads` before it's sent (`db.claim_upload`, `db.claim_clip_upload`), so a clip is never
  posted twice, even by overlapping runs. An upload that broke off after the video started going up may exist, so
  it's never retried automatically.

### Untrusted input
Stream titles, chat, transcripts, VOD metadata, web pages and **everything the AI returns** come from strangers.
- AI prompts: untrusted text goes inside the `<stream_data>` fence (see `ai.build_prompt`) and must not be able to
  close it. The clip-picking AI runs in an empty temp folder with **no tools that access files, run commands, or
  reach the network**. Claude Code uses `--tools ""`; the built-in Codex provider disables shell, file editing,
  apps, plugins and browser tools, ignores user configuration and uses a read-only sandbox. Its temporary model
  catalog must remove `apply_patch_tool_type` and model-specific extra tools. Only trend research gets web search,
  and its prompt contains no untrusted text. Never enable external tools for clip picking.
- AI output is validated before use (`ai.clean`): numbers parsed and clamped, text fields forced to capped strings.
- Untrusted text never reaches a shell. File names go through `util.slug`, folder names are checked against a strict
  pattern, subtitle text goes through `render.make_ass`'s escaping (strips `\ { }` and newlines).
- The dashboard page shows untrusted text as React text only (never `dangerouslySetInnerHTML` or `innerHTML`:
  `web/src/safety.test.ts` fails on them), links only http(s) URLs (`safeUrl`), and its server only answers its own
  page (Host and Origin checks, JSON-only writes, no inline scripts under its CSP). The dev server's proxy vouches for
  the dev page's Origin only (`web/vite.config.ts`); never loosen the Origin check in `dashboard.py` for it.
- Post text credits the streamer from yt-dlp's checked Twitch or Kick login (`pipeline.streamer`), added in code by
  `render.post_texts`: the AI is told not to write links or @mentions.

### Errors and logging
- Library code raises `ClipitError` for expected failures. Only `cli.py` calls `sys.exit`.
- One bad VOD must never stop a polling pass: `poll.py` catches it, logs it with its traceback, records it in the database.
- Log with `util.log` / `util.logger`, never `print`. Use `logger.exception` when a traceback helps.

### Code
- Python 3.11+, type hints on every public function (pyright checks them), imports sorted by ruff.
- Silence pyright only for wrong third-party stubs, with `# pyright: ignore[rule]` and the reason on the same line.
- New logic gets a pytest test in `tests/`, using fakes (`monkeypatch`, `tmp_path`) instead of the network.
- New dependency: add it to `pyproject.toml`, then regenerate the lockfile
  (`pip freeze --exclude-editable > requirements.lock`) and say why in the commit. For the page: `npm install <pkg>`
  in `web/` (it updates `package-lock.json`), and say why in the commit.
- Page code is TypeScript under `strict`, type-checked by `npm run build`. Never edit `clipit/web/` by hand: it's build output.
- Never commit `out/`, `.venv/`, `node_modules/`, credentials, browser profiles or tokens.
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
- Assumed one webcam position per VOD. Streamers switch scene layouts mid-stream (Asmongold: three), so layouts are
  found per VOD and chosen per clip. Test detection changes on more than one streamer.
- Trusted the AI to follow rules stated only in the prompt. A small local model ignored the clip-length rule, so the
  validator enforces it. Anything the prompt asks for that matters must also be checked in code.
- Let a failing signal degrade silently (chat download failed, clips were made without chat). Prefer failing the VOD
  so it's retried over quietly producing worse output.
- Assumed AI CLIs print clean text. `ollama run` writes terminal cursor codes even into a pipe; output is sanitized.
