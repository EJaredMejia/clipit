"""Decide which streams to clip and run the pipeline on each one's latest finished VOD.

Big live streamers first, then a few smaller ones (they go viral too). Each VOD is claimed in the
SQLite catalog before processing, so overlapping runs never double-process and finished VODs are never redone.
"""

import datetime
import random
import sqlite3
from pathlib import Path

from clipit import db, twitch
from clipit.pipeline import process_vod
from clipit.util import ROOT, log, logger, utcnow

SMALL = (300, 2000)  # viewer range for "smaller streamers": below ~300 there's too little chat to find moments
MAX_AGE = datetime.timedelta(days=2)  # older VODs aren't worth clipping anymore


def candidates(streams: list[tuple[str, int, str]], lang: str, rng=random) -> list[tuple[str, str]]:
    """(tier, login) in priority order: big streamers biggest first, then smaller streamers in random order."""
    streams = [s for s in streams if not lang or s[2] == lang]
    small = [s[0] for s in streams if SMALL[0] <= s[1] <= SMALL[1]]
    rng.shuffle(small)
    return [("top", s[0]) for s in streams if s[1] > SMALL[1]] + [("small", login) for login in small]


def ended(vod: dict) -> datetime.datetime:
    return datetime.datetime.fromisoformat(vod["publishedAt"]) + datetime.timedelta(seconds=vod["lengthSeconds"])


def poll(conn: sqlite3.Connection, top_n: int = 6, small_per_day: int = 4, lang: str = "EN", dry_run: bool = False,
         out_root: Path = ROOT / "out", **pipeline_opts) -> None:
    """One polling pass. Safe to run on a timer, and even twice at once."""
    now = utcnow()
    small_left = small_per_day - db.small_done_since(conn, now - datetime.timedelta(days=1))
    top_left = top_n
    for tier, login in candidates(twitch.live_streams(), lang):
        if (top_left if tier == "top" else small_left) <= 0:
            continue
        vod = twitch.latest_vod(login)
        if not vod or now - ended(vod) > MAX_AGE:
            continue  # no usable VOD (VODs off, corporate channel...): the next streamer takes the slot
        if tier == "top":
            top_left -= 1  # an already-clipped VOD still fills its slot, so we don't drift down the list
        vod_id, url = f"v{vod['id']}", f"https://www.twitch.tv/videos/{vod['id']}"
        if dry_run:
            if db.status(conn, vod_id) != "done":
                log(f"would clip {tier} streamer {login}: {url}")
                if tier == "small":
                    small_left -= 1
            continue
        if not db.claim(conn, vod_id, url, login, tier):
            continue
        log(f"clipping {tier} streamer {login}: {url}")
        if tier == "small":
            small_left -= 1
        try:
            _, clips = process_vod(url, out_root=out_root, **pipeline_opts)
            db.save_clips(conn, vod_id, [{**c, "file": f"{vod_id}/{c['file']}"} for c in clips])
            db.finish(conn, vod_id)
        except Exception as e:  # one bad VOD (sub-only, deleted...) must not stop the rest
            logger.exception("VOD %s failed", vod_id)
            db.finish(conn, vod_id, error=f"{type(e).__name__}: {e}"[:1000])
        finally:  # the ~300 MB audio is only needed while processing; a later rerun re-downloads it
            for p in (Path(out_root) / vod_id).glob("audio.*"):
                p.unlink()
