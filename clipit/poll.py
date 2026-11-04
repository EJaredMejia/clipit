"""Decide which streams to clip and run the pipeline on each one's latest finished VOD, on Twitch and Kick.

An AI searches the web first for who something just happened to. Then the streamers getting the most clip views today,
alternating between sites, then big live streamers. A pass goes down that list until it has a clip good enough to post
and stops there, since a run posts one clip: the ones asked for by name are all clipped, everyone the search found is
tried whatever top_n says, and past top_n at most EXTRA more VODs. For someone the search found, the VOD of the day
it happened is clipped, not just their latest, and the clip-picking AI is told what happened. Each VOD is claimed in the SQLite catalog before
processing, so overlapping runs never double-process and finished VODs are never redone.
"""

import datetime
import itertools
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

from clipit import ai, db, kick, runs, twitch
from clipit.pipeline import process_vod
from clipit.util import ROOT, ClipitError, ended, log, logger, utcnow

MIN_VIEWERS = 2000  # smaller streamers' clips don't get views
MAX_AGE = datetime.timedelta(days=2)  # older VODs aren't worth clipping anymore
# The VOD of an event the search found: people are still talking about it, so it's worth clipping a while longer.
EVENT_AGE = datetime.timedelta(days=ai.HOT_DAYS + 1)
EXTRA = 6  # more VODs a pass may try to find a clip good enough to post: bounded, each one takes minutes and AI calls
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
    for group, least in ((clipped, 0), (streams, MIN_VIEWERS)):
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


def find_hot(search_ai: str | None) -> dict[tuple[str, str], tuple[str, datetime.date | None]]:
    """{(site, login): (what happened, the day it did)} of who to clip today, best first. A failed search still leaves
    the pass the clip-view ranking."""
    if not search_ai:
        logger.warning("no AI with web search: going by clip views only")
        return {}
    try:
        found = ai.find_hot(search_ai)
    except Exception:
        logger.exception("the search for who's popping off failed: going by clip views only")
        return {}
    log(f"the search found {len(found)} streamer(s) with something happening")
    for site, login, why, when in found:
        log(f"  {site}:{login} ({when or 'day unknown'}): {why}")
    return {(site, login): (why, when) for site, login, why, when in found if login not in BLOCKED | NOT_STREAMERS}


def moments_of(site: ModuleType, login: str, vod: dict) -> list[tuple[int, int]]:
    """Where people clipped this VOD. Failing to find out only loses a hint: chat and loudness still pick moments."""
    try:
        found = site.clip_moments(login, vod)
    except Exception:
        logger.warning("could not get the clips of %s", login, exc_info=True)
        return []
    log(f"{len(found)} clipped moment(s) in {login}'s VOD")
    return found


def poll(
    conn: sqlite3.Connection,
    top_n: int = 6,
    lang: str = "EN",
    dry_run: bool = False,
    out_root: Path = ROOT / "out",
    prioritize: Iterable[tuple[str, str]] = (),
    search_ai: str | None = None,
    **pipeline_opts,
) -> int:
    """One polling pass. Safe to run on a timer, and even twice at once. prioritize: (site, name) clipped first.
    search_ai: the AI command with web search that finds who's popping off (ai.find_hot).
    Returns how many VODs failed if none worked: one broken VOD among good ones is logged, not a failed pass."""
    now = utcnow()
    failed = 0
    top_left = top_n
    extra_left, good, tried = EXTRA, False, 0
    runs.progress(what="Searching what's popping off today")
    hot = find_hot(search_ai)
    runs.progress(what="Finding streamers to clip")
    prioritize = list(prioritize)
    first = list(dict.fromkeys([*prioritize, *hot]))
    clipped = everywhere("top_clipped")
    try:
        live = live_streams()
    except ClipitError:
        if not first and not clipped:
            raise
        live = []  # both live lists down (each logged): the other streamers' VODs may still be reachable
    picks = candidates(live, lang, clipped, first)
    log(f"streamers to try: {', '.join(s[0] for s in picks[:10]) or 'none'}")
    if not clipped:
        logger.warning("no clip ranking from any site, going by live viewers")
    for login, _, _, site in picks:
        name = (site.__name__.rsplit(".", 1)[1], login.lower())
        is_hot = name in hot
        why, when = hot.get(name, ("", None))
        if good and name not in prioritize:
            continue
        if top_left <= 0 and not is_hot and (dry_run or extra_left <= 0):
            continue  # not a break: someone the search found may still be further down the list
        try:
            vod = site.latest_vod(login, when)
            if when and vod and db.status(conn, site.vod_ref(login, vod)[0]) == "done":
                vod = site.latest_vod(login)  # the event's VOD is clipped: their newest may have what came next
            # only a VOD from the event on gets EVENT_AGE: vod_on falls back to the newest when none covers the day
            event_vod = when and vod and ended(vod).date() >= when
            if not vod or now - ended(vod) > (EVENT_AGE if event_vod else MAX_AGE):
                if is_hot:  # the search's best leads must never vanish without a word
                    log(f"skipping {login}, found by the search: no {'VOD' if not vod else 'recent enough VOD'}")
                continue  # no usable VOD (VODs off, corporate channel...): the next streamer takes the slot
        except Exception:  # one streamer's VOD list failing (API change, odd data) must not end the pass
            logger.warning("could not list VODs of %s", login, exc_info=True)
            continue
        vod_id, url = site.vod_ref(login, vod)
        if dry_run:
            if db.status(conn, vod_id) != "done":
                top_left -= 1
                log(f"would clip {login}: {url}")
            continue
        if not db.claim(conn, vod_id, url, login, "top", run_id=runs.current()):
            if is_hot:
                log(f"skipping {login}, found by the search: this VOD was already clipped")
            continue  # already clipped: the next streamer takes the slot, so a repeat pass finds someone new
        if top_left > 0:
            top_left -= 1
        elif not is_hot:
            extra_left -= 1
        tried += 1
        log(f"clipping {login}: {url}")
        runs.progress(
            what=f"Clipping {login}", vod=[tried, max(tried, top_n, len(hot))], stage="Getting the VOD's details", count=None
        )
        try:
            with db.heartbeat(conn, vod_id):
                _, clips = process_vod(url, out_root=out_root, clipped=moments_of(site, login, vod), event=why, **pipeline_opts)
            db.save_clips(conn, vod_id, [{**c, "file": f"{vod_id}/{c['file']}"} for c in clips])
            db.finish(conn, vod_id)
            good = good or any(c["score"] >= db.AUTO_SCORE for c in clips)
        except Exception as e:  # one bad VOD (sub-only, deleted...) must not stop the rest
            logger.exception("VOD %s failed", vod_id)
            db.finish(conn, vod_id, error=f"{type(e).__name__}: {e}"[:1000])
            failed += 1
        finally:  # the ~300 MB audio is only needed while processing; a later rerun re-downloads it
            for p in (Path(out_root) / vod_id).glob("audio.*"):
                p.unlink()
    if top_n > 0 and not good and not dry_run:
        logger.warning("no clip scored %d+ this pass: nothing from it is good enough to post", db.AUTO_SCORE)
    if failed:
        logger.warning("%d of %d VODs failed to clip this pass (see above)", failed, tried)
    return failed if failed == tried else 0
