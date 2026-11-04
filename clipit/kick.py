"""Kick through the JSON API its website uses (no keys): chat replay, live streams, VOD lists.

Unofficial like Twitch's GQL: Kick can change it without notice. Failures surface as ClipitError / logged warnings.
"""

import datetime
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from clipit.twitch import MIN_CLIP_VIEWS, RETRY_WAITS
from clipit.util import ClipitError, logger, vod_on

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"  # Kick answers 403 to urllib's default user agent
PAGES = 5  # 100 live streams a page, most viewers first: 5 pages reach down to the smaller streamers
CLIP_PAGES = 5  # 20 clips a page, most viewed first: by page 5 they are down to ~80 views
MIN_FOLLOWERS = 100_000  # a clip can blow up on a nobody: seen 19,000 views for a channel of 1,023 followers
MAX_PAGES = 300  # chat pages to sample a VOD: ~4 min at GAP, alongside the audio download
CLOSE_SPOTS, CLOSE_SPAN, CLOSE_STEP = 40, 106, 15  # read_closer: ~280 more pages, ~4 min. Replaying 10 VODs' chat, this
# found as many of the good clips as 1500 pages spread evenly (20 of 38 against 19), in half the requests
GAP = 0.75  # seconds between requests: Kick answered 429 after 182 requests at ~2.2/s, and held 400 at 1.3/s
_turn = threading.Lock()
_next = 0.0


def wait_turn() -> None:
    """Space every request (all threads) GAP apart, so a long chat replay never trips Kick's rate limit."""
    global _next
    with _turn:
        now = time.monotonic()
        at, _next = max(now, _next), max(now, _next) + GAP
    time.sleep(at - now)


def get(url: str) -> Any:
    """GET a Kick API URL and return the decoded JSON, retrying network errors and garbled responses with backoff."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for wait in RETRY_WAITS:
        try:
            wait_turn()
            return json.load(urllib.request.urlopen(req, timeout=30))
        except (OSError, ValueError):  # HTTPError (429 too) is an OSError
            time.sleep(wait)
    wait_turn()
    return json.load(urllib.request.urlopen(req, timeout=30))  # last attempt: its error propagates


def check_slug(slug: str) -> str:
    """Channel slugs come from the API and go into URLs and post text: letters, digits, _ and - only."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", slug):
        raise ClipitError(f"unexpected Kick channel {slug!r}")
    return slug


def live_streams() -> list[tuple[str, int, str]]:
    """Live streams as (slug, viewers, language), most viewers first. Language upper-cased like Twitch's."""
    found, cursor = [], ""
    for _ in range(PAGES):
        d = get(f"https://web.kick.com/api/v1/livestreams?limit=100&sort=viewer_count_desc{cursor}")["data"]
        found += [(s["channel"]["slug"], s["viewer_count"], (s.get("language") or "").upper()) for s in d["livestreams"]]
        if not d.get("pagination", {}).get("next_cursor"):
            break
        cursor = "&after=" + urllib.parse.quote(d["pagination"]["next_cursor"])
    return sorted({s[0]: s for s in found if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", s[0])}.values(), key=lambda s: -s[1])


def top_clipped() -> list[tuple[str, int, str]]:
    """Streamers with MIN_CLIP_VIEWS on their clips of the last day as (slug, views, language). Kick's clips have no
    language or followers, so both are read with each one's latest VOD (two requests). Left out: under MIN_FOLLOWERS, or
    no usable VOD to clip anyway."""
    views: dict[str, int] = {}
    cursor = ""
    for _ in range(CLIP_PAGES):
        d = get(f"https://kick.com/api/v2/clips?sort=view&time=day{cursor}")
        for c in d["clips"]:
            slug = (c.get("channel") or {}).get("slug") or ""  # a deleted channel's clip must not lose the ranking
            if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", slug):
                views[slug] = views.get(slug, 0) + c["views"]
        if not d.get("nextCursor"):
            break
        cursor = "&cursor=" + urllib.parse.quote(d["nextCursor"])
    found = []
    for slug, n in sorted((s for s in views.items() if s[1] >= MIN_CLIP_VIEWS), key=lambda s: -s[1]):
        try:
            vod = latest_vod(slug)
        except Exception:  # one banned or odd channel must not lose the whole ranking
            logger.warning("could not read the language of %s", slug, exc_info=True)
            continue
        if vod and vod["followers"] >= MIN_FOLLOWERS:
            found.append((slug, n, vod["language"]))
    return found


def followers(channel: dict) -> int:
    """A channel's followers, a string in Kick's reply. Only the ranking needs them: an odd value must not stop clipping."""
    try:
        return int(channel.get("followers_count") or 0)
    except (TypeError, ValueError):
        logger.warning("unexpected Kick follower count %r", channel.get("followers_count"))
        return 0


def latest_vod(slug: str, on: datetime.date | None = None) -> dict | None:
    """Newest finished public VOD as {id, publishedAt, lengthSeconds, language, followers}, or None (none yet, still
    live, sub-only...). on: the newest one streaming that day instead, when there is one (util.vod_on)."""
    channel = get(f"https://kick.com/api/v2/channels/{check_slug(slug)}")
    vods = get(f"https://web.kick.com/api/v1/channels/{int(channel['id'])}/videos")["data"]
    found = [
        {
            "id": v["id"],
            "publishedAt": v["start_time"],
            "lengthSeconds": v["duration"],
            "language": (v.get("language") or "").split("-")[0].upper(),  # "en-IN" is English for --lang EN
        }
        for v in vods
        if not v["is_live"] and v["status"] == "public" and v["duration"] and v.get("start_time")
    ]
    # Kick lists some older VODs first (seen: Adin Ross's from Oct 16 ahead of Nov 3), so the newest wasn't found. Its
    # times all come as "2026-11-03T11:24:53Z": compared as text, so one odd one can't fail the whole channel.
    found.sort(key=lambda v: str(v["publishedAt"]), reverse=True)
    v = vod_on(found, on)
    return v and {**v, "followers": followers(channel)}


def clip_moments(slug: str, vod: dict) -> list[tuple[int, int]]:
    """Where in this VOD people clipped. shortcut: none yet, Kick's clips give no VOD offset, it has to be worked out from
    the playlist's timestamps; until then Kick VODs go by chat and loudness only."""
    return []


def vod_ref(slug: str, vod: dict) -> tuple[str, str]:
    """(catalog id, URL): the id is yt-dlp's, so it matches the pipeline's out/<id>/ folder."""
    return vod["id"], f"https://kick.com/{check_slug(slug)}/videos/{vod['id']}"


def text(content: str) -> str:
    return re.sub(r"\[emote:\d+:([^\]]*)\]", r"\1", content)  # "[emote:123:KEKW]" reads as "KEKW", like Twitch chat


def history(channel_id: int, before: int) -> dict:
    """One page of chat, the messages just before `before` (microseconds since the epoch), newest first."""
    return get(f"https://web.kick.com/api/v1/chat/{channel_id}/history?cursor={before}")["data"]


def read_chat(channel_id: int, start: float, a: float, end: float, step: float) -> list[list]:
    """The VOD's chat from second a to end. Where a page covers less than step seconds, the rest of that step is skipped:
    its messages are spread evenly over the step they stand for (steady chat stays a steady rate, not spikes at each
    page) and get a third field, the weight that keeps the rate right: [offset, text, weight]."""
    out, seen = [], set()
    cursor = int((start + end) * 1e6)
    while cursor > (start + a) * 1e6:  # pages go back in time: stop once one reaches the start
        d = history(channel_id, cursor)
        nxt = int(d.get("cursor") or 0)  # the oldest message's time; 0 at the start of the channel's chat
        if not d["messages"] or nxt >= cursor:
            break
        page = []
        for m in d["messages"]:
            t = datetime.datetime.fromisoformat(m["created_at"]).timestamp() - start
            if m["id"] not in seen and a <= t < end and m.get("content"):
                seen.add(m["id"])
                page.append([t, text(m["content"])])
        hi, covered = cursor / 1e6 - start, (cursor - nxt) / 1e6
        if covered < step and page:  # sampled: this page stands for [lo, hi)
            lo = max(a, hi - step)
            w = (hi - lo) / len(page)
            page = [[lo + (i + 0.5) * w, msg, (hi - lo) / covered] for i, (_, msg) in enumerate(sorted(page))]
        out += page
        cursor = min(nxt, cursor - int(step * 1e6))
    return out


def fetch_chat(channel_id: int, start: float, duration: float, slice_len: int = 300) -> list[list]:
    """A sample of a VOD's chat as [offset_seconds, text], in parallel slices, read_chat's weights where it skipped.

    A page holds only ~25 messages, about 1 s of a big streamer's chat (Adin Ross: 3.5 h of requests for 5 h of VOD),
    and Kick's rate limit makes each cost GAP seconds, so at most MAX_PAGES pages are read. read_closer then adds detail
    where it counts, once loudness says where to look.
    """
    step = max(1.0, duration / MAX_PAGES)  # seconds of VOD one page may stand for
    slices = [(a, min(a + slice_len, duration)) for a in range(0, math.ceil(duration), slice_len)]
    with ThreadPoolExecutor(4) as ex:  # wait_turn sets the pace; threads only overlap request latency
        return [m for part in ex.map(lambda s: read_chat(channel_id, start, s[0], s[1], step), slices) for m in part]


def read_closer(channel_id: int, start: float, chat: list[list], duration: float, spots: list[int]) -> list[list]:
    """chat (fetch_chat's sample) with the stretches around spots, the seconds moments would be picked at, read closer.
    A stretch the sample read in full is left alone. Spots must be CLOSE_SPAN apart."""
    step = min(CLOSE_STEP, max(1.0, duration / MAX_PAGES))
    spans = [(max(0, t - CLOSE_SPAN / 2), min(duration, t + CLOSE_SPAN / 2)) for t in spots]
    spans = [(a, b) for a, b in spans if any(len(m) > 2 and a <= m[0] < b for m in chat)]
    with ThreadPoolExecutor(4) as ex:
        closer = list(ex.map(lambda s: read_chat(channel_id, start, s[0], s[1], step), spans))
    kept = [m for m in chat if not any(a <= m[0] < b for a, b in spans)]
    return sorted([*kept, *(m for part in closer for m in part)], key=lambda m: m[0])
