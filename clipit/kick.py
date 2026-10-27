"""Kick through the JSON API its website uses (no keys): chat replay, live streams, VOD lists.

Unofficial like Twitch's GQL: Kick can change it without notice. Failures surface as ClipitError / logged warnings.
"""

import datetime
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from clipit.twitch import RETRY_WAITS
from clipit.util import ClipitError

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"  # Kick answers 403 to urllib's default user agent
PAGES = 5  # 100 live streams a page, most viewers first: 5 pages reach down to the smaller streamers
MAX_PAGES = 1500  # chat pages per VOD: ~19 min at GAP, alongside the audio download
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


def latest_vod(slug: str) -> dict | None:
    """Newest finished public VOD as {id, publishedAt, lengthSeconds}, or None (none yet, still live, sub-only...)."""
    channel = get(f"https://kick.com/api/v2/channels/{check_slug(slug)}")["id"]
    vods = get(f"https://web.kick.com/api/v1/channels/{int(channel)}/videos")["data"]
    v = next((v for v in vods if not v["is_live"] and v["status"] == "public" and v["duration"]), None)
    return v and {"id": v["id"], "publishedAt": v["start_time"], "lengthSeconds": v["duration"]}


def vod_ref(slug: str, vod: dict) -> tuple[str, str]:
    """(catalog id, URL): the id is yt-dlp's, so it matches the pipeline's out/<id>/ folder."""
    return vod["id"], f"https://kick.com/{check_slug(slug)}/videos/{vod['id']}"


def text(content: str) -> str:
    return re.sub(r"\[emote:\d+:([^\]]*)\]", r"\1", content)  # "[emote:123:KEKW]" reads as "KEKW", like Twitch chat


def history(channel_id: int, before: int) -> dict:
    """One page of chat, the messages just before `before` (microseconds since the epoch), newest first."""
    return get(f"https://web.kick.com/api/v1/chat/{channel_id}/history?cursor={before}")["data"]


def fetch_chat(channel_id: int, start: float, duration: float, slice_len: int = 300) -> list[list]:
    """Chat for a VOD that started at `start` (epoch seconds) as [offset_seconds, text], in parallel slices.

    A page holds only ~25 messages, about 1 s of a big streamer's chat (Adin Ross: 3.5 h of requests for 5 h of VOD).
    So at most MAX_PAGES pages are read: where a page covers less than `step` seconds, the rest of that step is skipped
    and its messages get a third field, the weight that keeps the chat rate right ([offset, text, weight]).
    """
    step = max(1.0, duration / MAX_PAGES)  # seconds of VOD one page may stand for

    def one_slice(a):
        out, seen, cursor = [], set(), int((start + a + slice_len) * 1e6)
        while cursor > (start + a) * 1e6:  # pages go back in time: stop once one reaches the slice start
            d = history(channel_id, cursor)
            nxt = int(d.get("cursor") or 0)  # the oldest message's time; 0 at the start of the channel's chat
            if not d["messages"] or nxt >= cursor:
                break
            covered = (cursor - nxt) / 1e6
            weight = [step / covered] if covered < step else []
            for m in d["messages"]:
                t = datetime.datetime.fromisoformat(m["created_at"]).timestamp() - start
                if m["id"] not in seen and a <= t < a + slice_len and m.get("content"):
                    seen.add(m["id"])
                    out.append([t, text(m["content"]), *weight])
            cursor = min(nxt, cursor - int(step * 1e6))
        return out

    with ThreadPoolExecutor(4) as ex:  # wait_turn sets the pace; threads only overlap request latency
        return [m for part in ex.map(one_slice, range(0, int(duration) + 1, slice_len)) for m in part]
