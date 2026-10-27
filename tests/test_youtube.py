import datetime

import pytest

from clipit import db, youtube
from clipit.util import ClipitError

CLIP = {
    "idx": 1,
    "file": "v1/clips/01_a.mp4",
    "start": 1.0,
    "end": 20.0,
    "title": "Asmon <script> " + "x" * 200,
    "hook": "h",
    "caption": "c",
    "youtube": "Asmon <3 this\n\nCredit: twitch.tv/zackrawrr",
    "streamer": "zackrawrr",
    "trend": "",
    "sound": "",
    "score": 8.0,
    "why": "",
}
VIDEO = {"id": "abc123", "snippet": {"channelTitle": "My clips"}, "status": {"privacyStatus": "public"}}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "_credentials", lambda: "creds")
    c = db.connect(tmp_path / "clipit.db")
    db.record_manual(c, "v1", "u")
    db.save_clips(c, "v1", [CLIP])
    db.set_verdict(c, c.execute("SELECT id FROM clips").fetchone()[0], "keep")
    yield c
    c.close()


def test_posts_a_kept_clip_once_and_records_the_link(conn, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(youtube, "_upload", lambda creds, video, body: sent.append((video, body)) or VIDEO)
    assert youtube.upload_next(conn, tmp_path) == "https://youtube.com/shorts/abc123"
    assert youtube.upload_next(conn, tmp_path) is None  # posted already, and too soon for another post
    ((video, body),) = sent
    assert video == tmp_path / CLIP["file"]
    assert body["snippet"]["title"] == "Asmon script " + "x" * 87  # no < >, capped at 100
    assert body["snippet"]["description"] == "Asmon 3 this\n\nCredit: twitch.tv/zackrawrr"
    assert body["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    assert db.overview(conn)["clips"][0]["uploads"]["youtube"]["url"] == "https://youtube.com/shorts/abc123"


def test_a_failed_upload_is_recorded_for_a_retry(conn, tmp_path, monkeypatch):
    def boom(*_):
        raise RuntimeError("quota")

    monkeypatch.setattr(youtube, "_upload", boom)
    with pytest.raises(RuntimeError):
        youtube.upload_next(conn, tmp_path)
    row = conn.execute("SELECT status, attempts, error FROM uploads").fetchone()
    assert tuple(row) == ("failed", 1, "RuntimeError: quota")


def test_without_a_login_nothing_is_claimed(conn, tmp_path, monkeypatch):
    monkeypatch.undo()  # the real _credentials
    monkeypatch.setattr(youtube, "TOKEN", tmp_path / "missing.json")
    with pytest.raises(ClipitError, match="youtube-login"):
        youtube.upload_next(conn, tmp_path)
    assert conn.execute("SELECT count(*) FROM uploads").fetchone()[0] == 0


def test_an_upload_that_may_be_up_is_never_retried_on_its_own(conn, tmp_path, monkeypatch):
    def broke_off(*_):
        raise youtube.MaybePosted("connection reset")

    monkeypatch.setattr(youtube, "_upload", broke_off)
    with pytest.raises(youtube.MaybePosted):
        youtube.upload_next(conn, tmp_path)
    assert tuple(conn.execute("SELECT status, attempts FROM uploads").fetchone()) == ("failed", db.MAX_ATTEMPTS)
    monkeypatch.setattr(youtube, "_upload", lambda *_: VIDEO)
    assert youtube.upload_next(conn, tmp_path) is None  # not even after the retry wait
    (cid,) = conn.execute("SELECT id FROM clips").fetchone()
    assert youtube.upload_clip(conn, cid, tmp_path)  # the user can still post it by hand, after checking


def test_a_description_the_user_emptied_stays_empty(conn, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(youtube, "_upload", lambda creds, video, body: sent.append(body) or VIDEO)
    (cid,) = conn.execute("SELECT id FROM clips").fetchone()
    db.set_text(conn, cid, "youtube", "")
    youtube.upload_next(conn, tmp_path)
    assert sent[0]["snippet"]["description"] == ""


def test_only_an_upload_that_started_sending_counts_as_maybe_posted(monkeypatch, tmp_path):
    class Request:
        resumable_uri: str | None = None

        def execute(self, num_retries):
            raise OSError("connection reset")

    request = Request()
    api = type("Api", (), {"videos": lambda self: self, "insert": lambda self, **kw: request})()
    monkeypatch.setattr(youtube, "build", lambda *a, **k: api)
    monkeypatch.setattr(youtube, "MediaFileUpload", lambda *a, **k: None)
    with pytest.raises(OSError) as e:  # failed before YouTube opened an upload session: nothing went up
        youtube._upload(youtube.Credentials(token=None), tmp_path / "v.mp4", {})
    assert not isinstance(e.value, youtube.MaybePosted)
    request.resumable_uri = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=x"
    with pytest.raises(youtube.MaybePosted):
        youtube._upload(youtube.Credentials(token=None), tmp_path / "v.mp4", {})


def test_hourly_uploads_wait_a_full_hour_and_keep_the_rolling_daily_limit(conn, tmp_path, monkeypatch):
    now = [datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)]
    start = now[0]
    monkeypatch.setattr(db, "utcnow", lambda: now[0])
    monkeypatch.setattr(youtube, "utcnow", lambda: now[0])
    db.save_clips(
        conn,
        "v1",
        [
            {**CLIP, "idx": i + 2, "file": f"v1/clips/{i + 2:02d}_a.mp4", "start": 100.0 * i + 100, "end": 100.0 * i + 120}
            for i in range(3)
        ],
    )
    for row in conn.execute("SELECT id FROM clips"):
        db.set_verdict(conn, row[0], "keep")
    sent = []

    def upload(creds, video, body):
        sent.append(video)
        return {**VIDEO, "id": f"post{len(sent)}"}

    monkeypatch.setattr(youtube, "_upload", upload)
    assert youtube.upload_next(conn, tmp_path) == "https://youtube.com/shorts/post1"
    now[0] = start + datetime.timedelta(minutes=59, seconds=59)
    assert youtube.upload_next(conn, tmp_path) is None
    now[0] = start + datetime.timedelta(hours=1)
    assert youtube.upload_next(conn, tmp_path) == "https://youtube.com/shorts/post2"
    now[0] = start + datetime.timedelta(hours=2)
    assert youtube.upload_next(conn, tmp_path) == "https://youtube.com/shorts/post3"
    now[0] = start + datetime.timedelta(hours=3)
    assert youtube.upload_next(conn, tmp_path) is None  # the fourth kept clip must wait for daily capacity
    assert len(sent) == 3
    now[0] = start + datetime.timedelta(hours=24, seconds=1)
    assert youtube.upload_next(conn, tmp_path) == "https://youtube.com/shorts/post4"
    assert len(set(sent)) == 4  # each eligible clip is sent once
