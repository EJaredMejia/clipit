"""Decide which streams to clip and run the pipeline on each one's latest finished VOD, on Twitch and Kick.

Big live streamers first, then a few smaller ones (they go viral too). Each VOD is claimed in the
SQLite catalog before processing, so overlapping runs never double-process and finished VODs are never redone.
"""

import datetime
import random
import sqlite3
from pathlib import Path
from types import ModuleType

from clipit import db, kick, twitch
from clipit.pipeline import process_vod
from clipit.util import ROOT, ClipitError, log, logger, utcnow

SMALL = (300, 2000)  # viewer range for "smaller streamers": below ~300 there's too little chat to find moments
MAX_AGE = datetime.timedelta(days=2)  # older VODs aren't worth clipping anymore


Stream = tuple[str, int, str, ModuleType]  # (login, viewers, language, site module: twitch or kick)
SITES = (kick, twitch)


def live_streams() -> list[Stream]:
    """Live streams of every site, most viewers first. A site that's down is skipped, so the others still get clipped."""
    found: dict[str, Stream] = {}
    for site in SITES:  # twitch last: a streamer live on both (simulcast) is clipped once, from Twitch
        try:
            found |= {login.lower(): (login, v, lang, site) for login, v, lang in site.live_streams()}
        except Exception:
            logger.warning("could not list live streams on %s", site.__name__, exc_info=True)
    if not found:
        raise ClipitError("could not list live streams on any site")
    return sorted(found.values(), key=lambda s: -s[1])


def candidates(streams: list[Stream], lang: str, rng: random.Random | None = None) -> list[tuple[str, Stream]]:
    """(tier, stream) in priority order: big streamers biggest first, then smaller streamers in random order."""
    streams = [s for s in streams if not lang or s[2] == lang]
    small = [s for s in streams if SMALL[0] <= s[1] <= SMALL[1]]
    (rng or random.Random()).shuffle(small)
    return [("top", s) for s in streams if s[1] > SMALL[1]] + [("small", s) for s in small]


def ended(vod: dict) -> datetime.datetime:
    return datetime.datetime.fromisoformat(vod["publishedAt"]) + datetime.timedelta(seconds=vod["lengthSeconds"])


def poll(
    conn: sqlite3.Connection,
    top_n: int = 6,
    small_per_day: int = 4,
    lang: str = "EN",
    dry_run: bool = False,
    out_root: Path = ROOT / "out",
    **pipeline_opts,
) -> None:
    """One polling pass. Safe to run on a timer, and even twice at once."""
    now = utcnow()
    small_left = small_per_day - db.small_done_since(conn, now - datetime.timedelta(days=1))
    top_left = top_n
    for tier, (login, _, _, site) in candidates(live_streams(), lang):
        if (top_left if tier == "top" else small_left) <= 0:
            continue
        try:
            vod = site.latest_vod(login)
            if not vod or now - ended(vod) > MAX_AGE:
                continue  # no usable VOD (VODs off, corporate channel...): the next streamer takes the slot
        except Exception:  # one streamer's VOD list failing (API change, odd data) must not end the pass
            logger.warning("could not list VODs of %s", login, exc_info=True)
            continue
        if tier == "top":
            top_left -= 1  # an already-clipped VOD still fills its slot, so we don't drift down the list
        vod_id, url = site.vod_ref(login, vod)
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
            with db.heartbeat(conn, vod_id):
                _, clips = process_vod(url, out_root=out_root, **pipeline_opts)
            db.save_clips(conn, vod_id, [{**c, "file": f"{vod_id}/{c['file']}"} for c in clips])
            db.finish(conn, vod_id)
        except Exception as e:  # one bad VOD (sub-only, deleted...) must not stop the rest
            logger.exception("VOD %s failed", vod_id)
            db.finish(conn, vod_id, error=f"{type(e).__name__}: {e}"[:1000])
        finally:  # the ~300 MB audio is only needed while processing; a later rerun re-downloads it
            for p in (Path(out_root) / vod_id).glob("audio.*"):
                p.unlink()
