export const REPO = 'https://github.com/EJaredMejia/clipit'

export const DESCRIPTION =
  'Clipit is a free, open-source tool that turns hours-long Twitch and Kick streams into vertical clips for TikTok and YouTube Shorts, with captions, a hook and post text. Runs on your machine with the AI you already have.'

// Search results cut descriptions at about 155 characters.
export const SUMMARY =
  'Turn Twitch and Kick streams into vertical clips for TikTok and YouTube Shorts, with captions and post text. Free, open source, runs on your machine.'

// Where the site is served; canonical and share-image URLs must be absolute.
export const SITE_URL = 'https://clipit.jared-mejia.workers.dev'

// The real order the pipeline runs in (clipit/pipeline.py).
export const STEPS = [
  {
    title: 'Listen',
    body: 'Downloads only the audio, about 360 MB for a four-hour stream instead of 12 GB of video.',
  },
  {
    title: 'Find the moments',
    body: 'Scores every second by loudness and chat activity, and starts with the seconds viewers already clipped.',
  },
  {
    title: 'Pick the cuts',
    body: 'Whisper transcribes the best windows. Your AI reads them, picks the clips and writes the title and post text.',
  },
  {
    title: 'Render',
    body: '1080×1920 video with the facecam stacked over gameplay, word-by-word captions and a hook. Only those seconds of video are downloaded.',
  },
  {
    title: 'Review and post',
    body: 'Keep or skip each clip in the dashboard. The ones you keep go to YouTube Shorts through the official API, a few a day.',
  },
]

export const FEATURES = [
  {
    title: 'Clip one stream',
    body: 'Paste a Twitch or Kick VOD link. Get ready-to-post clips with TikTok and YouTube text that credits the streamer.',
  },
  {
    title: 'Clip who’s blowing up today',
    body: 'Searches for who something just happened to, ranks streamers by today’s clip views and clips their latest stream. Built to run on a timer.',
  },
  {
    title: 'Facecam layout',
    body: 'Finds the webcam overlay, even when the streamer switches scenes mid-stream, and stacks it above the gameplay.',
  },
  {
    title: 'High-effort edits',
    body: 'The AI adds jump cuts, punch-in zooms, sound effects and pop-up text, then reviews frames of its own edit until it holds up.',
  },
  {
    title: 'A dashboard for the rest',
    body: 'Watch every clip, keep or skip, copy post text in one click, re-edit a clip by describing the change.',
  },
  {
    title: 'Your machine, your AI',
    body: 'Works with Claude Code, Codex CLI, a local model through Ollama, or any other AI CLI. No account, no server, MIT licensed.',
  },
]

export const SPECS: [string, string][] = [
  ['Sources', 'Twitch and Kick VODs'],
  ['Output', '1080×1920 MP4 with captions'],
  ['Posts to', 'YouTube Shorts; TikTok text ready to paste'],
  ['Runs on', 'Windows, macOS, Linux'],
  ['License', 'MIT'],
]

export const AIS = ['Claude Code', 'Codex CLI', 'Ollama', 'Any AI CLI']

export const INSTALL = [
  'git clone https://github.com/EJaredMejia/clipit.git',
  'cd clipit && python3 -m venv .venv && source .venv/bin/activate',
  'pip install -c requirements.lock -e .',
  'clipit https://www.twitch.tv/videos/2906456464',
]

// at: the minute of the VOD each clip starts, three minutes wide on the timeline.
export const CLIPS = [
  { hook: 'He did NOT see that coming', words: ['wait', 'wait', 'WAIT', 'no', 'way'], hot: 2, score: 9.1, hue: 330, at: 37 },
  { hook: 'Chat called it 10 seconds early', words: ['bro', 'is', 'actually', 'cooked'], hot: 3, score: 8.6, hue: 210, at: 111 },
  { hook: 'The cop stop went sideways', words: ['sir', 'step', 'out', 'of', 'the', 'car'], hot: 1, score: 8.8, hue: 40, at: 186 },
]

// A made-up four-hour VOD: loudness and chat per minute, deterministic so server and client render the same.
export const VOD = Array.from({ length: 240 }, (_, i) => {
  const base = 0.28 + 0.12 * Math.sin(i / 7) + 0.08 * Math.sin(i / 2.3 + 1)
  const spike = [38, 39, 112, 113, 114, 187, 188].includes(i) ? 0.55 : 0
  const loud = Math.min(1, base + spike + 0.06 * Math.sin(i * 12.9898))
  const chat = Math.min(1, base * 0.8 + spike * 1.1 + 0.1 * Math.abs(Math.sin(i * 4.1414)))
  return { loud, chat }
})

