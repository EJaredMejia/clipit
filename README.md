# Clipit

Turn hours-long Twitch and Kick streams into short vertical clips for TikTok and YouTube Shorts.

Clipit finds the best moments of a VOD from chat activity and audio, has an AI pick the cuts and write the post text,
and renders ready-to-post 9:16 videos with captions. It works with whatever AI you have on your machine: Claude Code,
Codex CLI, a local model through Ollama, or any other AI CLI.

## Features

- **Clip one stream.** Give it a Twitch or Kick VOD link and get 1080x1920 clips with a hook, word-by-word captions, a title, and ready-to-paste TikTok and YouTube text that credits the streamer and adds hashtags.
- **Clip live streamers automatically.** Checks who's live on Twitch and Kick, picks the biggest streamers, and clips each one's latest finished stream. Built to run unattended on a timer.
- **Follow trends.** The AI researches what's working on TikTok and Shorts this week, and the clip picks and captions follow it.
- **Facecam layout.** Finds the streamer's webcam overlay and stacks it above the gameplay.
- **Light on bandwidth.** Downloads only the audio to find moments (about 360 MB for 4 hours instead of 12 GB of video), then only the seconds of video it renders.
- **Dashboard.** Watch every clip in your browser, mark it keep or skip, record where you posted it, and see which VODs failed and why.
- **Post to YouTube Shorts.** The clips you keep go up on your channel through YouTube's official API, a few a day.

## Requirements

- Python 3.11+
- [ffmpeg](https://ffmpeg.org) on your PATH (`brew install ffmpeg` on macOS, `winget install ffmpeg` on Windows)
- An AI CLI, see [Choosing an AI](#choosing-an-ai)

## Installation

```bash
git clone https://github.com/EJaredMejia/clipit.git
cd clipit
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -c requirements.lock -e .
```

This installs the `clipit` command, with every dependency pinned by `requirements.lock`.

Or, faster, with [uv](https://docs.astral.sh/uv/) (it still makes a `.venv`, and can fetch Python 3.11+ if you don't have it):

```bash
uv venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
uv pip install -c requirements.lock -e .
```

Skip `uv sync` and `uv run`: they resolve their own versions into a `uv.lock` instead of using `requirements.lock`.

### Choosing an AI

- **Claude Code** (preferred when installed): install it, run `claude` once and type `/login`.
- **Codex CLI**: install [Codex](https://learn.chatgpt.com/docs/codex/cli) and run `codex login` once, or use the CLI bundled with the Windows Codex desktop app. Select it explicitly with:
  ```bash
  clipit <vod-url> --ai codex
  clipit --refresh-trends --trends-ai codex
  ```
  Clipit selects Codex automatically when Claude Code is not installed. It uses your existing Codex login and the CLI's default model. A request runs in an empty temporary workspace with user configuration, shell commands, file-editing tools, apps, plugins and browser controls disabled; only trend research enables web search. Clip selection uses a JSON output schema. The CLI must support `exec --ignore-user-config` and `debug models --bundled`; unsupported versions stop with an update message instead of falling back to broader permissions.
- **A local model with [Ollama](https://ollama.com)**:
  ```bash
  ollama pull qwen3:8b
  clipit <vod-url> --ai "ollama run qwen3:8b --think=false --format json --nowordwrap"
  ```
  - **Context window:** start the Ollama server with `OLLAMA_CONTEXT_LENGTH=32768`. The default window is much smaller than the ~10k-token clip-picking prompt, and Ollama silently cuts off whatever doesn't fit, so the model would never see most of the candidate moments.
  - **Flags:** `--think=false` and `--format json` make the reply plain JSON. `--nowordwrap` stops `ollama run` from writing terminal cursor codes into the reply (Clipit strips them anyway).
  - **Model size:** pick the biggest model your RAM allows (e.g. `qwen3:30b` with 32 GB).

## Usage

### Clip one stream

```bash
clipit https://www.twitch.tv/videos/2906456464
clipit https://www.twitch.tv/videos/2906456464 -n 10 --whisper turbo
```

### High-effort clips

```bash
clipit https://www.twitch.tv/videos/2906456464 -n 2 --high-effort
```

Takes the normal picks and puts much more work into each one. The AI edits it: jump cuts over dead air and filler,
punch-in zooms on reactions, sound effects (boom, whoosh, ding, pop, made by ffmpeg, no sound files), pop-up text and
highlighted words in the captions. Then a fresh AI call reviews that version against one goal, a really good edit that
trends, scores it, lists what's still wrong and returns an improved version. Clipit also checks every version itself
for dead air and a slow open. The loop goes on until a review scores the edit 9/10 with nothing left to fix, or after
5 reviews, when the best-reviewed version wins. Up to 5 AI calls per clip, so keep `-n` small.

The edited clips go next to the plain ones (`clips/..._edited.mp4`, `posts_edited.md`) and are added to the
dashboard; the plain clips stay as they were. Every review is kept in `edit/` to see what the AI changed and why.
In the dashboard, tick *High-effort edits* in the Run settings, or ask for a change to one clip with
[Re-edit with AI](#dashboard):

```bash
clipit --reedit 42 --request "a ding and a punch-in when he sees the second card" --at 6
```

### Clip live streamers automatically

```bash
clipit --poll --dry-run    # only show which VODs it would clip
clipit --poll
```

Each pass:

1. Reads Twitch's live streams (the top 30 overall plus the top 30 of each of the 30 biggest categories, about 800
   streams) and Kick's top 500. A name live on both sites is clipped on both, since the same name can belong to two
   different people: skip a simulcast's repeats in the dashboard. If one site's API is down, the pass goes on with the
   other.
2. **Big streamers:** the 6 biggest with at least 2,000 viewers (`--top`), after any trending ones. For each, it clips their latest *finished* VOD, ignoring a stream that's still live. A channel with no usable VOD gives its slot to the next one.
3. Skips VODs that ended more than 2 days ago and anything already done. Only English streams by default (`--lang ""` for any).
4. Each VOD is claimed in the database before processing, so two overlapping passes never do the same one. Finished VODs are never redone. Failed ones retry up to 3 times, at least an hour apart, and a pass that crashed mid-VOD is taken over 30 minutes after it stopped refreshing its claim. Downloaded audio is deleted after each VOD to save disk.

Run it every 30-60 minutes: either leave `clipit --poll --every 45` running (one pass, then waits 45 minutes, forever;
a failed pass is logged and the next still runs), or put `clipit --poll` on a timer (launchd on macOS, Task Scheduler
on Windows), which also survives reboots. A pass with new VODs takes about 10 minutes per VOD. Logs go to
`out/logs/clipit.log`.

### Refresh trends

```bash
clipit --refresh-trends
```

Has the AI search the web for this week's TikTok and Shorts trends and rewrites `trends.md`. Run it weekly, or edit
`trends.md` by hand.

### Dashboard

```bash
clipit --dashboard
```

Opens `http://127.0.0.1:8765` with every VOD and clip from `out/clipit.db`. It updates on its own while `--poll`
runs in the background.

- **Run:** click *Run now* to clip the latest VODs and then post the next due clip to YouTube (`clipit --poll`,
  then `clipit --upload`), and watch it go: each step, what it's doing now (streamer, VOD 2 of 6, transcribing 3/12)
  and its output. A notice pops up when it ends, with how many clips it made and posted. The run goes on if you close
  the dashboard, and shows again when you reopen it. *Stop* ends clipping right away, ffmpeg and all, but lets a
  YouTube post that's going finish first, so it's never cut off half sent. *Run settings* below it set what the next
  runs do: how many streamers, the most clips per VOD, the language, streamers to clip and post first, high-effort edits, and
  whether to post at all. They're saved, and apply to runs from the dashboard (a `--poll` on a timer keeps its flags).
- **Re-edit with AI:** in any clip, say what to change ("a boom when he slams the desk") and click *Re-edit*. Pause the
  video on the moment you mean and click *Mark this moment* first: the AI hears the speech and reads the chat, but
  can't see the video. The AI edits the clip again and reviews it until it does what you asked (clipit checks that the
  edit changed something near the second you marked), then renders a new video in place of the old one. A plain clip
  gets an edited version of its own. It runs as a run, in the Run tab; a clip already on YouTube can't be re-edited.
- **Clips:** play each one, filter by streamer or search, sort by AI score, and copy the YouTube title, YouTube
  description and TikTok caption with one click.
- **Review:** mark a clip *Keep* or *Skip*. The tiles at the top count what's left to review and what you kept but
  haven't posted yet.
- **Posted:** after posting, click *Mark posted* for TikTok or YouTube, with the post link if you have it, so a clip
  is never posted twice.
- **VODs:** every processed VOD with its status, and the error for the ones that failed.

It only listens on your own machine (127.0.0.1) and refuses requests from other websites.

### Post to YouTube Shorts

Clipit posts clips from VODs polled in the last 2 days (older trends have passed) to your channel, through YouTube's
official API: the ones you marked **Keep** first, then ones you haven't reviewed that the AI scored 8/10 or more,
newest first, public, at most 10 in any 24-hour period and at least 1 hour apart. Clips you skipped are never posted.
YouTube's default API quota covers about 6 uploads a day. Past that, a post waits for the quota to reset, without
using up the clip's tries. For all 10, ask for more quota in Google Cloud (APIs & Services, YouTube Data API v3, Quotas).

Set up once (free, about 15 minutes):

1. In the [Google Cloud console](https://console.cloud.google.com), create a project and enable **YouTube Data API v3**.
2. In **Google Auth Platform**, set up the consent screen as **External** and, under **Data Access**, add the scope
   `https://www.googleapis.com/auth/youtube.upload`.
3. The app starts in **Testing**, where only Google accounts listed as test users can sign in. Either:
   - in **Audience** → **Test users**, add the Google account that owns the YouTube channel. Without this,
     `clipit --youtube-login` fails with "Access blocked". In Testing, Google ends the login 7 days after you sign in,
     so posting stops until you run `--youtube-login` again, every week; or
   - fill in **Branding** (app name, support email), then in **Audience** click **Publish app** so it says
     **In production**. The login then lasts. Google may ask to verify apps that use the YouTube upload scope, but you
     don't need that to use it yourself: click through the "Google hasn't verified this app" warning when you sign in.
4. In **Clients**, create a **Desktop app** client, download its JSON and save it as `~/.clipit/youtube_client.json`
   (Windows: `C:\Users\<you>\.clipit\youtube_client.json`).
5. Run `clipit --youtube-login` and pick the channel to post to. The login is saved in `~/.clipit/youtube_token.json`.
   Keep both files private: anyone with them can post to your channel.

To post one clip right away, open it in the dashboard and click **Post to YouTube now**. That post counts toward the
pace of the automatic ones.

Then add `--upload` to the timer: `clipit --poll --upload`, or `clipit --poll --upload --every 45`. Each run posts at most one clip, and only when one is due.
Posted clips show in the dashboard with their link. A failed upload is retried on a later run, up to 3 times at least
an hour apart, and the error is in `out/logs/clipit.log`.

To keep the channel safe, don't raise the pace, don't post the same clip on several channels, and only post clips you
have the rights to: three copyright strikes delete a channel.

### Options

| Option | Default | Description |
|---|---|---|
| `URL` | | Twitch or Kick VOD link (`https://kick.com/<channel>/videos/<id>`). Other sites yt-dlp supports work too, without chat. |
| `-n`, `--clips N` | `6` | Most clips per VOD: the AI picks fewer, or none, when the VOD is weak. |
| `--high-effort` | | Edit every clip (cuts, zooms, sound effects, pop-up text) and have the AI review and improve it until it's done. See [High-effort clips](#high-effort-clips). |
| `--ai CMD` | Claude Code, then Codex | The AI that picks clips: `codex` uses the isolated built-in provider; other values are commands that read a prompt on stdin and print the answer. **Don't give custom commands tools**: prompts contain Twitch chat written by strangers. |
| `--trends-ai CMD` | Claude Code, then Codex | The AI for `--refresh-trends`, which needs web search. Use `codex` for Codex with web search only. |
| `--whisper MODEL` | `turbo` | Transcription model: `tiny`, `base`, `small`, `medium` or `turbo`. Bigger is more accurate but slower. |
| `--out DIR` | `out` | Where working files, clips, logs and the database go. |
| `--refresh-trends` | | Research current trends into `trends.md`. |
| `--poll` | | Pick live streamers and clip their latest finished VOD. |
| `--top N` | `6` | With `--poll`: how many of the biggest live streamers to clip. |
| `--lang CODE` | `EN` | With `--poll`: only streams in this language, `""` for any. |
| `--dry-run` | | With `--poll`: only print which VODs would be clipped. |
| `--dashboard` | | Open the dashboard in your browser. |
| `--port N` | `8765` | With `--dashboard`: the port on 127.0.0.1. |
| `--youtube-login` | | Sign in to the YouTube channel to post to. Once. |
| `--prioritize` | | `twitch:name` or `kick:name`: with `--poll`, clip their latest VOD first; with `--upload`, post their fresh clips first. Repeatable. For an event that's big right now, like `twitch:caedrel` during Worlds. |
| `--upload` | | Post a fresh clip you kept, or the AI scored 8+, to YouTube Shorts, if one is due. |
| `--reedit ID` | | Have the AI edit clip ID (its id in the dashboard) again until it does what `--request` asks. `--at S`: the second of the clip's video you mean. |

## Output

`out/<vod-id>/` holds the clips and the working files:

- `clips/01_<title>_<start>-<end>.mp4`: 1080x1920 with a hook box and word-by-word captions. Audio is normalized to -14 LUFS.
  - **Facecam on top, gameplay below** when a webcam overlay is on screen during that clip. Found automatically, per clip.
  - **Blurred fill + full frame** when there's no webcam, e.g. a camera-only or no-cam stream.
- `posts.md`: per clip, the title (for Shorts), the TikTok caption and YouTube description, the trend it fits, and a sound to add in the app.
  Both texts are the AI's description, then `Credit: twitch.tv/<streamer>` (or `kick.com/<streamer>`), then `#<streamer>` and 3-5 hashtags (YouTube also gets `#shorts`).
  FaZe creator clips always include `#stableronaldo` in both descriptions, without duplicates. This posting rule
  covers the known FaZe creators (including former members such as Lacy) and Twitch logins starting with `faze`;
  additional logins can be added to `FAZE_STREAMERS` in `clipit/render.py`.
  The credit is added by Clipit, not the AI, so chat can't trick it into tagging someone else.

`out/clipit.db` (SQLite) is the catalog: a `vods` table with every processed VOD and its status, a `clips` table
with every rendered clip (file, title, post text, streamer, score, your keep/skip), and an `uploads` table with where
each clip was posted.

## How it works

1. Downloads the **audio only**.
2. Pulls the **chat replay** and scores every second by chat speed (hype emotes like KEKW, LUL and "clip" count double), shifted 8 s earlier because chat reacts late, plus **loudness** peaks.
3. Takes the top `3 x clips` peaks and transcribes 90 s around each one with Whisper, locally.
4. Sends the transcripts, chat samples and `trends.md` to the AI. It returns exact cut points, a cold-open hook and the post text.
5. **Finds the facecam layouts** in 40 frames spread over the whole VOD. A webcam face recurs in the same spot for long stretches while game faces move, so each recurring spot against a frame edge is a webcam layout (streamers often have several, one per scene). Each crop snaps to that overlay's border, the one edge that stays put while the gameplay changes. Then, for every clip, a few frames from the clip itself decide which layout is on screen, or the blurred layout if none is.
6. ffmpeg **seeks straight into the stream** and downloads only those seconds of 1080p video while rendering.

Every step is cached in `out/<vod-id>/`, so reruns are fast. Delete `picks.json` to get new picks from the AI,
`facecam_layouts.json` to re-detect the webcam, or a clip to re-render it.

## Limitations

- Only post clips you have the rights to, or from streamers who allow clipping. Parts of Twitch VODs muted for DMCA stay silent in the clips.
- Webcam layouts are found from 40 frames across the VOD, so a layout used for under ~1/8 of the stream may be missed; clips from it get the blurred layout. A full-screen camera scene also gets the blurred layout.
- Gameplay is center-cropped, so text at the screen edges gets cut.
- Posting to TikTok is manual for now: mark clips posted in the dashboard. Add the trending sound inside TikTok, since burning music in gets clips muted.

## Roadmap

- [ ] Publish to TikTok, on one or more accounts
- [x] Publish to YouTube Shorts
- [ ] Publish to more than one YouTube channel

The publish commands will read the clips to post from the `clips` table in `out/clipit.db` and record each post in `uploads`.

## Contributing

```bash
pip install -c requirements.lock -e ".[dev]"   # or: uv pip install -c requirements.lock -e ".[dev]"
pre-commit install          # lint + format on every commit
ruff check . && ruff format --check . && pyright && pytest -q
```

The dashboard page is a React app in `web/`. Running clipit doesn't need Node: the built page is committed in
`clipit/web/`. To change the page you need Node 22.12+:

```bash
cd web && npm ci
npm run dev                 # http://localhost:5173, live reload, talks to a running clipit --dashboard
npm test && npm run build   # then commit clipit/web/ along with web/src/
```

All these checks also run in GitHub Actions on pushes to `main` and on pull requests (Linux and macOS). The code
layout, the coding conventions and the rules every change must follow are in [AGENTS.md](AGENTS.md).
