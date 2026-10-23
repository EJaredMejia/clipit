# clip-it

Give it an hours-long Twitch VOD. It returns vertical 9:16 clips ready for TikTok and YouTube Shorts, with a title, hook and caption for each. An AI of your choice picks the moments and checks them against current TikTok trends.

## Setup (once)

Needs Python 3.11+ and `ffmpeg` on PATH (`brew install ffmpeg` on a Mac, `winget install ffmpeg` on Windows).

```bash
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e ".[dev]"      # Windows: .venv/Scripts/pip
```

This installs a `clipit` command inside the venv, with every dependency pinned by `requirements.lock`.

You also need an AI CLI that's logged in. Claude Code works by default: install it, run `claude` once and type `/login`.
A local model works too: `--ai "ollama run qwen3:30b"`.

## Use

```bash
clipit https://www.twitch.tv/videos/2906456464
```

Options:

| flag | what it does |
|---|---|
| `-n 10` | number of clips (default 6) |
| `--ai "ollama run qwen3:30b"` | the AI that picks clips: any CLI that reads a prompt on stdin and prints the answer. The default is Claude Code with all tools disabled. **Don't give this command tools**: its prompt contains Twitch chat written by strangers. |
| `--trends-ai CMD` | the AI for `--refresh-trends`, which needs web search. The default is Claude Code with web tools only. |
| `--whisper turbo` | a bigger transcription model, more accurate but slower (default `small`) |
| `--refresh-trends` | has the AI search the web for this week's TikTok/Shorts trends and rewrites `trends.md`. Run it weekly, or edit `trends.md` by hand. |

## Automatic mode (polling)

```bash
clipit --poll --dry-run
clipit --poll
```

Each pass:
1. Reads Twitch's live streams: the top 30 overall plus the top 30 of each of the 30 biggest categories, about 800 streams.
2. **Big streamers:** the 6 biggest (`--top`). For each, it clips their latest *finished* VOD, ignoring a stream that's still live. A channel with no usable VOD gives its slot to the next one.
3. **Smaller streamers** (300-2,000 viewers): picked at random, at most 4 VODs a day (`--small`), so they're included without crowding out the big ones.
4. Skips VODs that ended more than 2 days ago and anything already done. Only English streams by default (`--lang ""` for any).
5. Each VOD is claimed in the database before processing, so two overlapping passes never do the same one. Finished VODs are never redone. Failed ones retry up to 3 times, at least an hour apart, and a pass that crashed mid-VOD is taken over 30 minutes after it stopped refreshing its claim. Downloaded audio is deleted after each VOD to save disk.

Run it on a timer (launchd on a Mac, Task Scheduler on Windows) every 30-60 minutes. A pass with new VODs takes about 10 minutes per VOD. Logs go to `out/logs/clipit.log`.

## What you get

`out/clipit.db` (SQLite) is the catalog: a `vods` table with every processed VOD and its status, and a `clips` table with every rendered clip (file, title, hook, caption, score). This is what the uploaders will read.

`out/<vod-id>/` holds the working files and the clips:
- `clips/01_<title>_<start>-<end>.mp4`: 1080x1920 with a hook box and word-by-word captions. Audio is normalized to -14 LUFS.
  - **Facecam on top, gameplay below** when the stream has a webcam overlay. The overlay is found automatically.
  - **Blurred fill + full frame** when there's no webcam, e.g. a camera-only or no-cam stream.
- `posts.md`: per clip, the title (for Shorts), a copy-paste caption with hashtags (for TikTok), the trend it fits, and a sound to add in the app.

## How it picks

1. Downloads the **audio only**: about 360 MB for 4 hours, instead of 12 GB of video.
2. Pulls the **chat replay** and scores every second by chat speed (hype emotes like KEKW, LUL and "clip" count double), shifted 8 s earlier because chat reacts late, plus **loudness** peaks.
3. Takes the top `3 x clips` peaks and transcribes 90 s around each one with Whisper, locally.
4. Sends the transcripts, chat samples and `trends.md` to the AI. It returns exact cut points, a cold-open hook and the post text.
5. **Finds the facecam** in 40 frames spread over the whole VOD. The webcam face stays in one spot for hours while game faces move, so the spot with the most face sightings wins. The crop then snaps to the overlay's border, the one edge that stays put while the gameplay changes.
6. ffmpeg **seeks straight into the stream** and downloads only those seconds of 1080p video while rendering.

Every step is cached in `out/<vod-id>/`, so reruns are fast. Delete `picks.json` to get new picks from the AI, `facecam.json` to re-detect the webcam, or a clip to re-render it.

## Code layout

`clipit/` has one module per pipeline stage:

| module | job |
|---|---|
| `cli.py` | command-line flags, then calls the pipeline |
| `pipeline.py` | `process_vod(url, ...)`: runs every stage with caching and returns `(folder, rendered clips)` |
| `db.py` | the SQLite catalog: schema migrations, claiming VODs, recording clips |
| `source.py` | yt-dlp: VOD info, audio download, stream URLs |
| `twitch.py` | Twitch's public API: chat replay, live streams, latest VODs |
| `poll.py` | picks which streamers to clip and runs the pipeline on each, never the same VOD twice |
| `moments.py` | loudness + chat scoring, picks candidate windows |
| `transcribe.py` | Whisper on the candidate windows |
| `ai.py` | prompts, calling the AI CLI, validating its picks, trend research |
| `facecam.py` | webcam overlay detection |
| `render.py` | captions, ffmpeg render, `posts.md` |
| `util.py` | logging, shell helpers with timeouts, atomic writes, `ClipitError` |

Coding conventions, and the rules every change must follow, are in [AGENTS.md](AGENTS.md).

Planned: **auto-upload to TikTok / Shorts on several accounts**, an `upload/` package that reads the `clips` table, with accounts and per-clip upload status as new tables.

## Notes

- Only post clips you have the rights to, or from streamers who allow clipping. Parts of Twitch VODs muted for DMCA stay silent in the clips.
- The facecam is detected once per VOD. If the streamer moves the cam or switches to a full-screen camera scene partway through, clips from those parts get the wrong crop. Gameplay is center-cropped, so text at the screen edges gets cut.
- Upload is manual for now. Add the trending sound inside TikTok, since burning music in gets clips muted.
- Development: `ruff check .` and `pytest -q`, also run by GitHub Actions on every push (Linux and macOS).
