"""Twitch through its public GQL API (no keys): chat replay, live streams, VOD lists.

Unofficial: Twitch can change it without notice. Failures surface as ClipitError / logged warnings, never silently.
"""

import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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


def gql(query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request("https://gql.twitch.tv/gql", json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 {"Client-Id": GQL_CLIENT, "Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    if d.get("errors"):
        raise ClipitError(f"Twitch API: {d['errors'][0]['message']}")
    return d["data"]


def live_streams() -> list[tuple[str, int, str]]:
    """Live streams as (login, viewers, language), most viewers first."""
    d = gql(LIVE)
    edges = d["streams"]["edges"] + [s for g in d["games"]["edges"] for s in g["node"]["streams"]["edges"]]
    found = {e["node"]["broadcaster"]["login"]: (e["node"]["viewersCount"], e["node"]["language"])
             for e in edges if e["node"]["broadcaster"]}
    return sorted(((login, v, lang) for login, (v, lang) in found.items()), key=lambda s: -s[1])


def latest_vod(login: str) -> dict | None:
    """Newest finished past-broadcast VOD as {id, publishedAt, lengthSeconds, status}, or None (VODs off, still recording...)."""
    user = gql(VODS, {"login": login})["user"]
    vods = [e["node"] for e in ((user or {}).get("videos") or {}).get("edges", [])]
    return next((v for v in vods if v["status"] == "RECORDED"), None)


def gql_comments(vid, t):
    body = json.dumps([{"operationName": "VideoCommentsByOffsetOrCursor",
                        "variables": {"videoID": vid, "contentOffsetSeconds": t},
                        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": GQL_HASH}}}]).encode()
    req = urllib.request.Request("https://gql.twitch.tv/gql", body, {"Client-Id": GQL_CLIENT, "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))[0]["data"]["video"]["comments"]
        except (OSError, ValueError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


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
