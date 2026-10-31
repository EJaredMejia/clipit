"""Twitch through its public GQL API (no keys): chat replay, live streams, VOD lists.

Unofficial: Twitch can change it without notice. Failures surface as ClipitError / logged warnings, never silently.
"""

import json
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from clipit.util import ClipitError

# Twitch's public web client id + persisted chat query. Cursor paging fails Twitch's integrity check; offset paging works.
GQL_CLIENT = "kimne78kx3ncx6brgo4mv6wki5h1ko"
GQL_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

# Twitch blocks paging past the first 30 streams without a browser token, so the top streams of the top 30
# categories are how we reach smaller streamers (~800 streams in one call).
LIVE = """query { streams(first: 30) { edges { node { viewersCount language broadcaster { login } } } }
  games(first: 30) { edges { node { streams(first: 30) { edges { node { viewersCount language broadcaster { login } } } } } } } }"""
CLIPS = """query { games(first: 30) { edges { node {
  clips(first: 30, criteria: {period: LAST_DAY, sort: VIEWS_DESC}) { edges { node { viewCount language broadcaster { login followers { totalCount } } } } } } } } }"""
VODS = """query($login: String!) {
  user(login: $login) { videos(first: 3, type: ARCHIVE, sort: TIME) { edges { node { id publishedAt lengthSeconds status } } } } }"""


MIN_CLIP_VIEWS = 1000  # views on a streamer's clips of the last day: less is no sign people want their clips
MIN_FOLLOWERS = 1_000_000  # a clip can blow up on a nobody: seen 11,000 views for a channel of 4,851 followers
RETRY_WAITS = (2, 4, 8, 16, 30, 30)  # seconds between attempts: rides out ~90 s of network trouble (seen: DNS failures)


def errors(reply: Any) -> list[str]:
    """The error messages in a GQL reply, a single response or a batch."""
    return [
        str(e.get("message", e)) if isinstance(e, dict) else str(e)
        for r in (reply if isinstance(reply, list) else [reply])
        if isinstance(r, dict)
        for e in r.get("errors") or []
    ]


def post(body: dict | list) -> Any:
    """POST to Twitch GQL and return the decoded JSON, retrying network errors, garbled responses and Twitch's own
    passing failures ("service error", "service timeout": seen in a live run) with backoff."""
    req = urllib.request.Request(
        "https://gql.twitch.tv/gql",
        json.dumps(body).encode(),
        {"Client-Id": GQL_CLIENT, "Content-Type": "application/json"},
    )
    for wait in RETRY_WAITS:
        try:
            reply = json.load(urllib.request.urlopen(req, timeout=30))
            if not any(m.startswith("service ") for m in errors(reply)):
                return reply
        except (OSError, ValueError):
            pass
        time.sleep(wait)
    return json.load(urllib.request.urlopen(req, timeout=30))  # last attempt: its error is the caller's to raise


def gql(query: str, variables: dict | None = None) -> dict:
    d = post({"query": query, "variables": variables or {}})
    if e := errors(d):
        raise ClipitError(f"Twitch API: {e[0]}")
    return d["data"]


def live_streams() -> list[tuple[str, int, str]]:
    """Live streams as (login, viewers, language), most viewers first."""
    d = gql(LIVE)
    edges = d["streams"]["edges"] + [s for g in d["games"]["edges"] for s in g["node"]["streams"]["edges"]]
    found = {
        e["node"]["broadcaster"]["login"]: (e["node"]["viewersCount"], e["node"]["language"])
        for e in edges
        if e["node"]["broadcaster"]
    }
    return sorted(((login, v, lang) for login, (v, lang) in found.items()), key=lambda s: -s[1])


def top_clipped() -> list[tuple[str, int, str]]:
    """Streamers with MIN_FOLLOWERS and MIN_CLIP_VIEWS on their clips of the last day, across the 30 biggest categories,
    as (login, views, the language with most of those views)."""
    views: dict[str, Counter[str]] = {}
    for game in gql(CLIPS)["games"]["edges"]:
        for e in (game["node"]["clips"] or {}).get("edges") or []:
            who = e["node"]["broadcaster"]
            if who and ((who.get("followers") or {}).get("totalCount") or 0) >= MIN_FOLLOWERS:
                views.setdefault(who["login"], Counter())[e["node"]["language"]] += e["node"]["viewCount"]
    found = ((login, v.total(), v.most_common(1)[0][0]) for login, v in views.items())
    return sorted((s for s in found if s[1] >= MIN_CLIP_VIEWS), key=lambda s: -s[1])


def latest_vod(login: str) -> dict | None:
    """Newest finished past-broadcast VOD as {id, publishedAt, lengthSeconds, status}, or None (VODs off, still recording...)."""
    user = gql(VODS, {"login": login})["user"]
    vods = [e["node"] for e in ((user or {}).get("videos") or {}).get("edges", [])]
    return next((v for v in vods if v["status"] == "RECORDED"), None)


def vod_ref(login: str, vod: dict) -> tuple[str, str]:
    """(catalog id, URL): the id is yt-dlp's, so it matches the pipeline's out/<id>/ folder."""
    return f"v{vod['id']}", f"https://www.twitch.tv/videos/{vod['id']}"


def gql_comments(vid: str, t: int) -> dict | None:
    body = [
        {
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": {"videoID": vid, "contentOffsetSeconds": t},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": GQL_HASH}},
        }
    ]
    reply = post(body)
    if e := errors(reply):  # e.g. PersistedQueryNotFound: Twitch retired the chat query's hash
        raise ClipitError(f"Twitch chat API: {e[0]}")
    try:
        return reply[0]["data"]["video"]["comments"]
    except (KeyError, IndexError, TypeError):
        raise ClipitError(f"Twitch chat API: unexpected reply {str(reply)[:200]}") from None


def fetch_chat(vid: str, duration: float, slice_len: int = 300) -> list[list]:
    """All chat messages as [offset_seconds, text], fetched in parallel time slices."""

    def one_slice(a):
        b, t, seen, out = a + slice_len, a, set(), []
        while t < b and t <= duration:  # Twitch answers "service error" past the VOD's end, even if the last page had more
            c = gql_comments(vid, t)
            if not c or not c["edges"]:
                break
            for e in c["edges"]:
                n = e["node"]
                if n["id"] not in seen and a <= n["contentOffsetSeconds"] < b:
                    seen.add(n["id"])
                    out.append([n["contentOffsetSeconds"], "".join(f["text"] or "" for f in n["message"]["fragments"] or [])])
            if not c["pageInfo"]["hasNextPage"]:
                break
            last = c["edges"][-1]["node"]["contentOffsetSeconds"]
            t = last if last > t else t + 1  # shortcut: drops the rest of a second with >1 page of messages, fine for counting
        return out

    with ThreadPoolExecutor(12) as ex:
        return [m for part in ex.map(one_slice, range(0, int(duration) + 1, slice_len)) for m in part]
