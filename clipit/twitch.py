"""Twitch through its public GQL API (no keys): chat replay, live streams, VOD lists.

Unofficial: Twitch can change it without notice. Failures surface as ClipitError / logged warnings, never silently.
"""

import json
import time
import urllib.request
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
VODS = """query($login: String!) {
  user(login: $login) { videos(first: 3, type: ARCHIVE, sort: TIME) { edges { node { id publishedAt lengthSeconds status } } } } }"""


RETRY_WAITS = (2, 4, 8, 16, 30, 30)  # seconds between attempts: rides out ~90 s of network trouble (seen: DNS failures)


def post(body: dict | list) -> Any:
    """POST to Twitch GQL and return the decoded JSON, retrying network errors and garbled responses with backoff."""
    req = urllib.request.Request(
        "https://gql.twitch.tv/gql",
        json.dumps(body).encode(),
        {"Client-Id": GQL_CLIENT, "Content-Type": "application/json"},
    )
    for wait in RETRY_WAITS:
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except (OSError, ValueError):
            time.sleep(wait)
    return json.load(urllib.request.urlopen(req, timeout=30))  # last attempt: its error propagates


def gql(query: str, variables: dict | None = None) -> dict:
    d = post({"query": query, "variables": variables or {}})
    if d.get("errors"):
        raise ClipitError(f"Twitch API: {d['errors'][0]['message']}")
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
    return post(body)[0]["data"]["video"]["comments"]


def fetch_chat(vid: str, duration: float, slice_len: int = 300) -> list[list]:
    """All chat messages as [offset_seconds, text], fetched in parallel time slices."""

    def one_slice(a):
        b, t, seen, out = a + slice_len, a, set(), []
        while t < b:
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
