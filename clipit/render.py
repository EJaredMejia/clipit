"""Turning a pick into a finished 1080x1920 mp4, and the posts.md that goes with the clips."""

import os
import re
import subprocess
from collections.abc import Collection
from pathlib import Path

from clipit import source
from clipit.facecam import TOP
from clipit.util import filter_words, mmss, sh, write_atomic

RENDER_TIMEOUT = 1800
MIN_RENDER = 1024  # bytes: an mp4 with no frames is about 260
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
Style: Pop,Arial Black,96,&H0000FFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,4,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def make_ass(
    hook: str,
    words: list,
    start: float,
    dur: float,
    hook_y: int,
    cap_y: int,
    pops: Collection[dict] = (),
    emphasis: Collection[str] = (),
) -> str:
    """Hook box + trending word-by-word captions (3-word chunks, current word yellow), centered on the given y.
    High-effort edits add pops ({start, end, text} in VOD seconds: pop-up text above the captions) and emphasis
    (lowercase words shown red in the captions)."""

    def t(x):
        h, r = divmod(max(0.0, x), 3600)
        m, s = divmod(r, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"

    def esc(s):
        s = re.sub("[\U0001f000-\U0001ffff☀-➿️]", "", s)  # emoji render as boxes in libass
        s = re.sub(r"\s+", " ", filter_words(s))[:120]  # a newline would end the Dialogue line and let text forge new ones
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

    def color(w: str, current: bool) -> str:
        if current:
            return f"{{\\c&H00FFFF&}}{w}{{\\c&HFFFFFF&}}"
        return f"{{\\c&H3C3CFF&}}{w}{{\\c&HFFFFFF&}}" if re.sub(r"[^\w'-]", "", w).lower() in emphasis else w

    for ch in chunks:
        for j, (a, b, _) in enumerate(ch):
            end = ch[j + 1][0] if j + 1 < len(ch) else b
            text = " ".join(color(w, k == j) for k, (_, _, w) in enumerate(ch))
            ev.append(f"Dialogue: 1,{t(a)},{t(end)},Cap,,0,0,0,,{{\\pos(540,{cap_y})}}{text}")
    for p in pops:  # pops in from 60% size
        if esc(p["text"]):
            pos = f"{{\\pos(540,{cap_y - 260})\\fscx60\\fscy60\\t(0,120,\\fscx100\\fscy100)}}"
            ev.append(f"Dialogue: 2,{t(p['start'] - start)},{t(p['end'] - start)},Pop,,0,0,0,,{pos}{esc(p['text'])}")
    return ASS_HEADER + "\n".join(ev) + "\n"


# Sound effects synthesized by ffmpeg: no sound files to license, download or trust.
SFX = {
    "boom": "aevalsrc='0.9*sin(2*PI*(110-70*t)*t)*exp(-4*t)':d=0.9",
    "whoosh": "anoisesrc=d=0.5:c=pink:a=0.4,highpass=f=500,afade=t=in:d=0.25,afade=t=out:st=0.25:d=0.25",
    "ding": "aevalsrc='0.35*sin(2*PI*1320*t)*exp(-5*t)':d=0.9",
    "pop": "aevalsrc='0.6*sin(2*PI*(900-1500*t)*t)*exp(-30*t)':d=0.2",
}
LOUDNORM = "loudnorm=I=-14:TP=-1.5"
# webcam over gameplay, the webcam alone, one full-screen 9:16 crop, the whole frame on a blur
LAYOUTS = ("split", "cam", "crop", "fill")
SHEET, SHEET_FPS = (6, 3), 2  # a contact sheet's columns and rows, and frames of each second: 9 s a sheet, faces still legible
STEREO = "aformat=sample_rates=48000:channel_layouts=stereo"


def edit_graph(edit: dict, start: float, subs: str, audio: int) -> str:
    """Filters for a high-effort edit (clipit.edit) of the frame labeled [base], times in VOD seconds; makes [v] and [a].
    Punch-in zooms come before the captions (subs) so those stay put. Cuts come last, on video and audio both, so
    captions, pop-ups and sound effects are cut along with what they belong to and stay in sync."""
    zooms, sfx, cuts = edit.get("zooms", []), edit.get("sfx", []), edit.get("cuts", [])
    graph, last = "", "base"
    if zooms:
        graph = f"[base]split={len(zooms) + 1}[o0]" + "".join(f"[z{i}]" for i in range(1, len(zooms) + 1)) + ";"
        for i, z in enumerate(zooms, 1):
            f, a, b = z["zoom"], z["start"] - start, z["end"] - start
            graph += f"[z{i}]crop=iw/{f:.2f}:ih/{f:.2f},scale=1080:1920,setsar=1[zz{i}];"
            graph += f"[o{i - 1}][zz{i}]overlay=enable='between(t,{a:.2f},{b:.2f})'[o{i}];"
        last = f"o{len(zooms)}"
    keep = "'not(" + "+".join(f"between(t,{c['start'] - start:.2f},{c['end'] - start:.2f})" for c in cuts) + ")'"
    graph += f"[{last}]{subs}" + (f",select={keep},setpts=N/FRAME_RATE/TB" if cuts else "") + "[v];"
    # after a seek, Twitch's audio can start ~2 s after the video: pad it from 0 so cuts hit the same moments in both
    graph += f"[{audio}:a]aresample=async=1:first_pts=0,{STEREO}[src];"
    for i, x in enumerate(sfx):
        graph += f"{SFX[x['sound']]},{STEREO},adelay={max(0, round((x['at'] - start) * 1000))}:all=1[s{i}];"
    graph += "[src]" + "".join(f"[s{i}]" for i in range(len(sfx)))
    graph += f"amix=inputs={len(sfx) + 1}:duration=first:normalize=0," if sfx else "anull,"
    return graph + (f"aselect={keep},asetpts=N/SR/TB," if cuts else "") + LOUDNORM + "[a]"


def frames(video: Path, times: list[float], folder: Path) -> list[tuple[float, Path]]:
    """(time, still) of a rendered clip at times (seconds into it), half size, as frame01.jpg... in folder."""
    out = []
    for i, t in enumerate(times, 1):
        path = folder / f"frame{i:02d}.jpg"
        sh(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-vf", "scale=540:-2"]
            + ["-q:v", "4", str(path)],
            timeout=60,
        )
        if path.exists():  # a time past the last frame makes no picture, without an error
            out.append((t, path))
    return out


def sheets(video: Path, folder: Path) -> list[tuple[float, Path]]:
    """The whole clip as contact sheets, sheet01.jpg... in folder, with the second each starts at: AI CLIs take pictures,
    not video, so SHEET_FPS frames of every second is how one watches a clip, motion included."""
    cols, rows = SHEET
    vf = f"fps={SHEET_FPS},scale=240:-2,tile={cols}x{rows}"
    sh(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", vf, "-q:v", "4", str(folder / "sheet%02d.jpg")], timeout=120)
    return [(i * cols * rows / SHEET_FPS, p) for i, p in enumerate(sorted(folder.glob("sheet*.jpg")))]


def source_frame(url: str, t: float, path: Path) -> None:
    """The stream itself at VOD second t, before any layout, with a line every 10% of its width: what the edit's
    focus is measured on."""
    vf = "scale=960:-2,drawgrid=w=iw/10:h=ih:t=2:c=yellow@0.8"
    with source.inputs([url], t, 1) as stream:
        sh(["ffmpeg", "-v", "error", "-y", *stream, "-frames:v", "1", "-vf", vf, str(path)], 120)


def centered(at: str, lo: str, hi: str) -> str:
    """A crop's x that centers it on at, kept inside lo-hi. Commas escaped: a bare one starts the next filter."""
    return f"clip({at}-ow/2\\,{lo}\\,{hi}-ow)"


def render(urls: list[str], clip: dict, words: list, out: Path, cam: list[float] | None = None) -> None:
    """Seek straight into the remote stream and render 1080x1920 with captions, in the edit's layout (LAYOUTS), by
    default facecam on top + gameplay below, or blurred fill + full frame when there's no facecam (pipeline.render_clip
    crops on a person filling the shot instead). The edit's focus
    (0-1, default the middle) is the middle of what to keep across the stream (across the webcam box for cam): the
    gameplay panel or the full-screen crop is centered on it, as far as the frame allows."""
    s, d = clip["start"], clip["end"] - clip["start"]
    ass, edit = out.with_suffix(".ass"), clip.get("edit")
    layout, focus = (edit or {}).get("layout") or ("split" if cam else "fill"), (edit or {}).get("focus", 0.5)
    if cam and layout == "split":
        x, y, w, h = cam
        graph = (
            f"[0:v]split[a][b];[a]crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f},scale={TOP[0]}:{TOP[1]},setsar=1[top];"
            f"[b]crop=ih*{TOP[0]}/{1920 - TOP[1]}:ih:{centered(f'iw*{focus:.2f}', '0', 'iw')}:0,scale={TOP[0]}:{1920 - TOP[1]},setsar=1[bot];"
            "[top][bot]vstack"
        )
        hook_y, cap_y = TOP[1], 1450  # hook sits on the seam, captions on the lower gameplay
    elif cam and layout == "cam":  # a 9:16 cut of the webcam box
        x, y, w, h = cam
        cw = f"min(iw*{w:.4f}\\,ih*{h:.4f}*9/16)"  # comma escaped, as in centered
        graph = (
            f"[0:v]crop={cw}:ih*{h:.4f}:{centered(f'iw*{x + w * focus:.4f}', f'iw*{x:.4f}', f'iw*{x + w:.4f}')}:ih*{y:.4f},"
            "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
        )
        hook_y, cap_y = 330, 1400
    elif layout == "crop":
        graph = f"[0:v]crop=ih*9/16:ih:{centered(f'iw*{focus:.2f}', '0', 'iw')}:0,scale=1080:1920,setsar=1"
        hook_y, cap_y = 330, 1400
    else:
        graph = (
            "[0:v]split[a][b];[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8,"
            "scale=1080:1920,eq=brightness=-0.12[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=0:(H-h)/2"
        )
        hook_y, cap_y = 540, 1400
    pops, emphasis = (edit or {}).get("texts", []), (edit or {}).get("emphasis", [])
    ass.write_text(make_ass(clip.get("hook", ""), words, s, d, hook_y, cap_y, pops, emphasis), encoding="utf-8")
    subs = f"subtitles={ass.name}"  # relative name: avoids Windows path escaping
    if edit:  # a high-effort clip: its edit mixes the sound in the graph too
        graph += "[base];" + edit_graph(edit, s, subs, len(urls) - 1)
        audio = ["-map", "[a]"]
    else:
        graph += f",{subs}[v]"
        audio = ["-map", f"{len(urls) - 1}:a", "-af", LOUDNORM]
    tmp = out.with_name(out.stem + ".tmp.mp4")
    try:
        with source.inputs(urls, s, d) as inputs:
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
                    *audio,
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
        if tmp.stat().st_size < MIN_RENDER:  # a stream that gives no frames still exits 0
            raise subprocess.CalledProcessError(1, "ffmpeg", stderr=f"{tmp.name} has no video")
        os.replace(tmp, out)
    finally:
        ass.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


def post_texts(clip: dict) -> tuple[str, str]:
    """(TikTok caption, YouTube description): the AI's description, a credit to the streamer, then the hashtags.
    The credit is added here, never by the AI, so injected chat can't make it tag or link someone else."""
    login = clip["streamer"]
    tag = login.replace("-", "")
    tags = ([tag] if tag else []) + [t for t in clip.get("hashtags", []) if t.lower() != tag.lower()]
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
    credit = f"Credit: {clip.get('site') or 'twitch.tv'}/{login}" if login else ""
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
