"""Post clips to YouTube Shorts through the official YouTube Data API.

Only clips from polled VODs of the last db.FRESH are posted: the ones the user marked Keep, then unreviewed ones the AI
scored db.AUTO_SCORE+, never skipped ones, at most PER_DAY a day and GAP apart: slow posting through the official API
is what keeps the channel clear of spam enforcement. Never replace this with browser automation of YouTube Studio.

Credentials live in ~/.clipit/, outside the repo and the out folder: the OAuth client the user downloads from
Google Cloud, and the token `clipit --youtube-login` saves.
"""

import datetime
import sqlite3
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from clipit import db
from clipit.util import ClipitError, log, logger, utcnow, write_atomic

HOME = Path.home() / ".clipit"
CLIENT = HOME / "youtube_client.json"
TOKEN = HOME / "youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]  # posting only: it can't read or manage the channel
PER_DAY = 3
GAP = datetime.timedelta(hours=1)
STUCK = datetime.timedelta(hours=2)  # an upload still unfinished this long after it started belongs to a run that died


def login() -> None:
    """One-time browser sign-in to the channel to post to. Saves a token that keeps working unattended."""
    if not CLIENT.exists():
        raise ClipitError(f"No OAuth client at {CLIENT}: download it from Google Cloud (README, Post to YouTube Shorts)")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    # prompt=consent: Google only hands out the long-lived refresh token when the consent screen is shown
    creds = flow.run_local_server(port=0, timeout_seconds=600, prompt="consent")
    if not creds.refresh_token:
        raise ClipitError("Google returned no refresh token: run clipit --youtube-login again")
    HOME.mkdir(exist_ok=True)
    HOME.chmod(0o700)  # owner only, also when the user made the folder: the token (and its temp file) live inside it
    write_atomic(TOKEN, creds.to_json())
    TOKEN.chmod(0o600)  # on Windows both only toggle read-only; the folder sits in the user's own profile there
    log(f"logged in to YouTube, token saved in {TOKEN}")


def metadata(clip: sqlite3.Row) -> dict:
    """The video's title, description and settings. YouTube rejects < and >, and titles over 100 characters."""

    def text(s: str) -> str:
        return s.replace("<", "").replace(">", "")

    return {
        "snippet": {
            "title": " ".join(text(clip["title"]).split())[:100] or "Stream clip",
            # clips made before descriptions only have the caption; once the user edits, an empty one is their choice
            "description": text(clip["youtube"] if clip["edited"] else clip["youtube"] or clip["caption"]),
            "categoryId": "20",  # Gaming
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }


def upload_next(conn: sqlite3.Connection, out: Path) -> str | None:
    """Post the next due clip (db.claim_upload) that isn't on YouTube yet, if the pacing allows. Returns its link."""
    for clip_id in db.stuck_uploads(conn, "youtube", utcnow() - STUCK):
        logger.warning(
            "clip %d was being posted to YouTube when a run stopped, so it may be up: check YouTube Studio, then in the "
            "dashboard add its link, or undo its YouTube post to have it posted again",
            clip_id,
        )
    creds = _credentials()  # before claiming: a broken login must not use up a clip's attempts
    clip = db.claim_upload(conn, "youtube", PER_DAY, GAP)
    if not clip:
        log("no YouTube post due: no fresh clip is kept or scored high enough, or the last post was too recent")
        return None
    return _post(conn, creds, clip, out)


def upload_clip(conn: sqlite3.Connection, clip_id: int, out: Path) -> str | None:
    """Post one clip now, because the user asked in the dashboard. None when it's posted or being posted already."""
    creds = _credentials()
    clip = db.claim_clip_upload(conn, clip_id, "youtube")
    return _post(conn, creds, clip, out) if clip else None


def _post(conn: sqlite3.Connection, creds: Credentials, clip: sqlite3.Row, out: Path) -> str:
    log(f"posting clip {clip['id']} to YouTube: {clip['title']}")
    try:
        video_file = (out / clip["file"]).resolve()
        if not video_file.is_relative_to(out.resolve()):  # never send a local file that isn't one of our clips
            raise ClipitError(f"clip file {clip['file']} is outside {out}")
        video = _upload(creds, video_file, metadata(clip))
    except Exception as e:  # retried on a later run (up to db.MAX_ATTEMPTS), unless the video may already be up
        db.fail_upload(conn, clip["id"], "youtube", f"{type(e).__name__}: {e}"[:1000], retry=not isinstance(e, MaybePosted))
        raise
    url = f"https://youtube.com/shorts/{video['id']}"
    db.set_upload(conn, clip["id"], "youtube", url)
    log(f"posted clip {clip['id']} to {video['snippet']['channelTitle']}: {url}")
    if (privacy := video["status"]["privacyStatus"]) != "public":
        logger.warning("YouTube made clip %d %s, not public: check it in YouTube Studio", clip["id"], privacy)
    return url


def _credentials() -> Credentials:
    if not TOKEN.exists():
        raise ClipitError("Not logged in to YouTube: run clipit --youtube-login")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    try:
        creds.refresh(Request())  # requests transport, 120 s timeout
    except RefreshError as e:
        raise ClipitError(
            f"The YouTube login stopped working ({e}): run clipit --youtube-login. If this happens every week, the "
            "Google Cloud app is still in Testing: publish it (In production)"
        ) from e
    return creds


class MaybePosted(ClipitError):
    """The upload broke off after the video started going up: YouTube may have it, so it's never retried on its own."""


def _upload(creds: Credentials, video: Path, body: dict) -> dict:
    # build() makes its own HTTP client with a 60 s socket timeout, and reads the API description bundled with the library
    youtube = build("youtube", "v3", credentials=creds)
    media = MediaFileUpload(str(video), mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    try:
        # retries resume the same upload session, so a retry inside this call never posts the video twice
        return request.execute(num_retries=3)
    except Exception as e:
        if request.resumable_uri:  # the session was open: the video may be up even though no answer came back
            raise MaybePosted(f"the upload broke off, so it may be on YouTube: check YouTube Studio ({e})") from e
        raise
