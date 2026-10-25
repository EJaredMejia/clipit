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
