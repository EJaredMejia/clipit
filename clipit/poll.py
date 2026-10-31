"""Decide which streams to clip and run the pipeline on each one's latest finished VOD, on Twitch and Kick.

Streamers getting the most clip views today first, alternating between sites, then big live streamers. Each VOD is claimed in the
SQLite catalog before processing, so overlapping runs never double-process and finished VODs are never redone.
"""

import datetime
import itertools
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

from clipit import db, kick, runs, twitch
from clipit.pipeline import process_vod
from clipit.util import ROOT, ClipitError, log, logger, utcnow

MIN_VIEWERS = 2000  # smaller streamers' clips don't get views
MAX_AGE = datetime.timedelta(days=2)  # older VODs aren't worth clipping anymore
# Never clipped, on any site, even when most clipped or prioritized: known to copyright-strike clip channels until banned.
BLOCKED = frozenset({"pokimane"})
# Tournament and company channels: famous and clipped, but no streamer to name. shortcut: the ones seen, add more as they show up.
NOT_STREAMERS = frozenset({"pgl", "riotgames"})


Stream = tuple[str, int, str, ModuleType]  # (login, viewers, language, site module: twitch or kick)
SITES = (kick, twitch)


def everywhere(listing: str) -> list[Stream]:
    """A site module's listing (live_streams, top_clipped) from every site, biggest first. A site that's down is skipped."""
    found: list[Stream] = []
    for site in SITES:
        try:
            found += [(login, n, lang, site) for login, n, lang in getattr(site, listing)()]
        except Exception:
            logger.warning("could not get %s of %s", listing, site.__name__, exc_info=True)
    return sorted(found, key=lambda s: -s[1])


def live_streams() -> list[Stream]:
    """Live streams of every site, most viewers first. A site that's down is skipped, so the others still get clipped."""
    # A name live on both sites may be two different people, so both are kept: a simulcast is clipped twice, and you
    # skip the repeats in the dashboard, rather than an unrelated Kick streamer never being clipped.
    found = everywhere("live_streams")
    if not found:
        raise ClipitError("could not list live streams on any site")
    return found


def mix(streams: list[Stream]) -> list[Stream]:
    """Alternate between sites, keeping each site's order, so Twitch's bigger numbers don't take every slot."""
    by_site = [[s for s in streams if s[3] is site] for site in SITES]
    return [s for group in itertools.zip_longest(*by_site) for s in group if s]


def candidates(
    streams: list[Stream], lang: str, clipped: Iterable[Stream] = (), first: Iterable[tuple[str, str]] = ()
) -> list[Stream]:
    """Streams in priority order: the ones asked for first (live or not, any language), then the most clip views of the
    last day (live or not), then live streamers with at least MIN_VIEWERS. Each group biggest first, alternating sites."""
    sites = {site.__name__.rsplit(".", 1)[1]: site for site in SITES}
    out: list[Stream] = [(name, 0, "", sites[site]) for site, name in first if name.lower() not in BLOCKED]
    for group, least in ((clipped, kick.MIN_CLIP_VIEWS), (streams, MIN_VIEWERS)):
        picked = {(s[0].lower(), s[3]) for s in out}  # someone in two groups would take two slots
        out += mix(
            [
                s
                for s in group
                if s[1] >= least
                and (not lang or s[2] == lang)
                and (s[0].lower(), s[3]) not in picked
                and s[0].lower() not in BLOCKED | NOT_STREAMERS
            ]
        )
    return out


def ended(vod: dict) -> datetime.datetime:
    return datetime.datetime.fromisoformat(vod["publishedAt"]) + datetime.timedelta(seconds=vod["lengthSeconds"])


def poll(
    conn: sqlite3.Connection,
    top_n: int = 6,
    lang: str = "EN",
    dry_run: bool = False,
    out_root: Path = ROOT / "out",
    prioritize: Iterable[tuple[str, str]] = (),
    **pipeline_opts,
) -> int:
    """One polling pass. Safe to run on a timer, and even twice at once. prioritize: (site, name) clipped first.
    Returns how many VODs failed: the rest are still tried, but the caller can tell the pass didn't go well."""
    now = utcnow()
    failed = 0
    top_left = top_n
    runs.progress(what="Finding streamers to clip")
    first = list(dict.fromkeys(prioritize))
    clipped = everywhere("top_clipped")
    log(f"most clipped today: {', '.join(f'{s[0]} ({s[1]} views)' for s in clipped[:10]) or 'unknown, going by live viewers'}")
    try:
        live = live_streams()
    except ClipitError:
        if not first and not clipped:
            raise
        live = []  # both live lists down (each logged): the other streamers' VODs may still be reachable
    for login, _, _, site in candidates(live, lang, clipped, first):
        if top_left <= 0:
            break
        try:
            vod = site.latest_vod(login)
            if not vod or now - ended(vod) > MAX_AGE:
                continue  # no usable VOD (VODs off, corporate channel...): the next streamer takes the slot
        except Exception:  # one streamer's VOD list failing (API change, odd data) must not end the pass
            logger.warning("could not list VODs of %s", login, exc_info=True)
            continue
        top_left -= 1  # an already-clipped VOD still fills its slot, so we don't drift down the list
        vod_id, url = site.vod_ref(login, vod)
        if dry_run:
            if db.status(conn, vod_id) != "done":
                log(f"would clip {login}: {url}")
            continue
        if not db.claim(conn, vod_id, url, login, "top", run_id=runs.current()):
            continue
        log(f"clipping {login}: {url}")
        runs.progress(what=f"Clipping {login}", vod=[top_n - top_left, top_n], stage="Getting the VOD's details", count=None)
        try:
            with db.heartbeat(conn, vod_id):
                _, clips = process_vod(url, out_root=out_root, **pipeline_opts)
            db.save_clips(conn, vod_id, [{**c, "file": f"{vod_id}/{c['file']}"} for c in clips])
            db.finish(conn, vod_id)
        except Exception as e:  # one bad VOD (sub-only, deleted...) must not stop the rest
            logger.exception("VOD %s failed", vod_id)
            db.finish(conn, vod_id, error=f"{type(e).__name__}: {e}"[:1000])
            failed += 1
        finally:  # the ~300 MB audio is only needed while processing; a later rerun re-downloads it
            for p in (Path(out_root) / vod_id).glob("audio.*"):
                p.unlink()
    return failed
