"""Turning a pick into a finished 1080x1920 mp4, and the posts.md that goes with the clips."""

import os
import re
from pathlib import Path

from clipit.facecam import TOP
from clipit.util import REMOTE, mmss, sh, write_atomic

RENDER_TIMEOUT = 1800
# Posting policy includes the creators historically associated with FaZe, regardless of later roster changes.
FAZE_STREAMERS = frozenset(
    {
        "stableronaldo",
        "lacy",
        "jasontheween",
        "silky",
        "adapt",
        "yourragegaming",
        "yourrage",
        "plaqueboymax",
        "banks",
        "rug",
        "rain",
        "apex",
        "temperrr",
        "swagg",
        "jsmooth",
        "kaysan",
        "natehill",
        "replays",
        "cloakzy",
    }
)

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1
Style: Hook,Arial Black,62,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,14,0,5,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def make_ass(hook: str, words: list, start: float, dur: float, hook_y: int, cap_y: int) -> str:
    """Hook box + trending word-by-word captions (3-word chunks, current word yellow), centered on the given y."""

    def t(x):
        h, r = divmod(max(0.0, x), 3600)
        m, s = divmod(r, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"

    def esc(s):
        s = re.sub("[\U0001f000-\U0001ffff☀-➿️]", "", s)  # emoji render as boxes in libass
        s = re.sub(r"\s+", " ", s)[:120]  # a newline would end the Dialogue line and let text forge new ones
        return s.replace("\\", "/").replace("{", "(").replace("}", ")").upper().strip()

    ev = [f"Dialogue: 0,{t(0)},{t(dur)},Hook,,0,0,0,,{{\\pos(540,{hook_y})}}{esc(hook)}"] if hook and esc(hook) else []
    chunks, cur = [], []
    for a, b, w in words:
        a, b, w = a - start, b - start, esc(w)
        if not w:
            continue
        if cur and (len(cur) == 3 or a - cur[-1][1] > 0.6):
            chunks.append(cur)
            cur = []
        cur.append((a, b, w))
    chunks += [cur] if cur else []
    for ch in chunks:
        for j, (a, b, _) in enumerate(ch):
            end = ch[j + 1][0] if j + 1 < len(ch) else b
            text = " ".join(f"{{\\c&H00FFFF&}}{w}{{\\c&HFFFFFF&}}" if k == j else w for k, (_, _, w) in enumerate(ch))
            ev.append(f"Dialogue: 1,{t(a)},{t(end)},Cap,,0,0,0,,{{\\pos(540,{cap_y})}}{text}")
    return ASS_HEADER + "\n".join(ev) + "\n"


def render(urls: list[str], clip: dict, words: list, out: Path, cam: list[float] | None = None) -> None:
    """Seek straight into the remote stream and render 1080x1920 with captions:
    facecam on top + center-cropped gameplay below, or blurred fill + full frame when there's no facecam."""
    s, d = clip["start"], clip["end"] - clip["start"]
    if cam:
        x, y, w, h = cam
        graph = (
            f"[0:v]split[a][b];[a]crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f},scale={TOP[0]}:{TOP[1]},setsar=1[top];"
            f"[b]crop=ih*{TOP[0]}/{1920 - TOP[1]}:ih,scale={TOP[0]}:{1920 - TOP[1]},setsar=1[bot];[top][bot]vstack"
        )
        hook_y, cap_y = TOP[1], 1450  # hook sits on the seam, captions on the lower gameplay
    else:
        graph = (
            "[0:v]split[a][b];[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8,"
            "scale=1080:1920,eq=brightness=-0.12[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=0:(H-h)/2"
        )
        hook_y, cap_y = 540, 1400
    ass = out.with_suffix(".ass")
    ass.write_text(make_ass(clip.get("hook", ""), words, s, d, hook_y, cap_y), encoding="utf-8")
    graph += f",subtitles={ass.name}[v]"  # relative name: avoids Windows path escaping
    inputs = [arg for u in urls for arg in (*REMOTE, "-ss", f"{s:.2f}", "-t", f"{d:.2f}", "-i", u)]
    tmp = out.with_name(out.stem + ".tmp.mp4")
    try:
        sh(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                *inputs,
                "-filter_complex",
                graph,
                "-map",
                "[v]",
                "-map",
                f"{len(urls) - 1}:a",
                "-af",
                "loudnorm=I=-14:TP=-1.5",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                tmp.name,
            ],
            timeout=RENDER_TIMEOUT,
            cwd=out.parent,
        )
        os.replace(tmp, out)
    finally:
        ass.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


def post_texts(clip: dict) -> tuple[str, str]:
    """(TikTok caption, YouTube description): the AI's description, a credit to the streamer, then the hashtags.
    The credit is added here, never by the AI, so injected chat can't make it tag or link someone else."""
    login = clip["streamer"]
    tags = ([login] if login else []) + [t for t in clip.get("hashtags", []) if t.lower() != login.lower()]
    description = clip["description"]
    if login.lower() == "kaicenat":
        tags = [t for t in tags if t.lower() not in {"amp", "rakai"}]
        tags.extend(["amp", "RaKai"])
        description = re.sub(r"\s*#(?:amp|rakai)\b", "", description, flags=re.IGNORECASE).strip()
    if login.lower() in FAZE_STREAMERS or login.lower().startswith("faze"):
        # Enforce this in code even when the AI omits it or returns a differently cased duplicate.
        tags = [t for t in tags if t.lower() != "stableronaldo"]
        tags.append("stableronaldo")
        description = re.sub(r"\s*#stableronaldo\b", "", description, flags=re.IGNORECASE).strip()
    credit = f"Credit: twitch.tv/{login}" if login else ""
    yt_tags = tags if "shorts" in map(str.lower, tags) else [*tags, "shorts"]

    def text(hashtags: list[str]) -> str:
        return "\n\n".join(filter(None, [description, credit, " ".join(f"#{t}" for t in hashtags)]))

    return text(tags), text(yt_tags)


def write_posts(path: Path, info: dict, url: str, picks: list[dict]) -> None:
    """Human-readable copy of the picks: titles and post text ready to paste."""
    md = [f"# Clips from: {info.get('title')}\n\n{url}\n"]
    for i, c in enumerate(picks, 1):
        md += [
            f"## {i}. {c.get('title')}  ({c['score']:g}/10)\n",
            f"- file: `{c['file']}`",
            f"- streamer: {c['streamer'] or '-'}",
            f"- from VOD {mmss(c['start'])} to {mmss(c['end'])} ({c['end'] - c['start']:.0f}s)",
            f"- hook: {c.get('hook')}",
            f"- trend: {c.get('trend')}",
            f"- sound to add in-app: {c.get('sound') or '-'}",
            f"- why: {c.get('why')}\n",
            "TikTok caption:\n",
            "```",
            c["caption"],
            "```\n",
            "YouTube description:\n",
            "```",
            c["youtube"],
            "```\n",
        ]
    write_atomic(path, "\n".join(md))
