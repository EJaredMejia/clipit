import datetime
import json

import httplib2
import pytest
from googleapiclient.errors import HttpError

from clipit import db, youtube
from clipit.util import ClipitError, utcnow

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
    db.claim(c, "v1", "u", "s", "top")
    db.finish(c, "v1")
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


def test_hitting_the_daily_quota_costs_the_clip_no_attempt(conn, tmp_path, monkeypatch):
    def over_quota(*_):
        raise HttpError(httplib2.Response({"status": "403"}), b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}')

    monkeypatch.setattr(youtube, "_upload", over_quota)
    with pytest.raises(HttpError):
        youtube.upload_next(conn, tmp_path)
    assert tuple(conn.execute("SELECT status, attempts FROM uploads").fetchone()) == ("failed", 0)


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
    monkeypatch.setattr(youtube, "PER_DAY", 3)  # the rolling window, not the configured number, is under test
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


def test_a_run_posts_one_clip_per_vod_first_the_rest_scheduled_an_hour_apart(conn, tmp_path, monkeypatch):
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    monkeypatch.setattr(db, "utcnow", lambda: now)
    monkeypatch.setattr(youtube, "utcnow", lambda: now)
    monkeypatch.setattr(db, "POSTS", 3)
    conn.execute("UPDATE vods SET claimed_at = ?", (now.isoformat(),))
    db.save_clips(conn, "v1", [{**CLIP, "idx": 2, "file": "v1/clips/02_a.mp4", "start": 50.0, "end": 70.0}])
    db.claim(conn, "v2", "u2", "s", "top")
    conn.execute("UPDATE vods SET claimed_at = ?", (now.isoformat(),))
    db.save_clips(conn, "v2", [{**CLIP, "file": "v2/clips/01_a.mp4"}])
    sent = []
    monkeypatch.setattr(youtube, "_upload", lambda creds, video, body: sent.append((video.parent.parent.name, body)) or VIDEO)
    assert youtube.upload_run(conn, tmp_path) == 3
    assert [v for v, _ in sent] == ["v1", "v2", "v1"]  # the kept clip, then a VOD not posted yet
    assert sent[0][1]["status"]["privacyStatus"] == "public"
    assert [b["status"] for _, b in sent[1:]] == [
        {"privacyStatus": "private", "publishAt": f"2026-01-01T0{h}:00:00+00:00", "selfDeclaredMadeForKids": False}
        for h in (1, 2)
    ]
    assert youtube.upload_run(conn, tmp_path) == 0  # the next run waits for an hour after the last scheduled one


def test_a_run_still_posts_them_all_when_an_upload_takes_a_while(conn, tmp_path, monkeypatch):
    # Real case: the first post was recorded when its upload ended, so the next slot an hour after the run's start
    # looked too soon and a run with three clips scored 8+ posted one.
    now = [datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)]
    monkeypatch.setattr(db, "utcnow", lambda: now[0])
    monkeypatch.setattr(youtube, "utcnow", lambda: now[0])
    monkeypatch.setattr(db, "POSTS", 3)
    db.save_clips(conn, "v1", [{**CLIP, "idx": 2, "file": "v1/clips/02_a.mp4", "start": 50.0, "end": 70.0}])
    db.claim(conn, "v2", "u2", "s", "top")
    conn.execute("UPDATE vods SET claimed_at = ?", (now[0].isoformat(),))
    db.save_clips(conn, "v2", [{**CLIP, "file": "v2/clips/01_a.mp4"}])
    publish = []

    def upload(creds, video, body):
        now[0] += datetime.timedelta(minutes=2)
        publish.append(body["status"].get("publishAt"))
        return VIDEO

    monkeypatch.setattr(youtube, "_upload", upload)
    assert youtube.upload_run(conn, tmp_path) == 3
    assert publish == [None, "2026-01-01T01:02:00+00:00", "2026-01-01T02:02:00+00:00"]


def test_a_run_that_runs_out_of_quota_keeps_its_posts_and_other_errors_still_fail(conn, tmp_path, monkeypatch):
    db.save_clips(conn, "v1", [{**CLIP, "idx": 2, "file": "v1/clips/02_a.mp4", "start": 50.0, "end": 70.0}])
    sent = []

    def upload(creds, video, body):
        if sent:
            raise HttpError(httplib2.Response({"status": 403}), b'{"error": {"errors": [{"reason": "quotaExceeded"}]}}')
        sent.append(video)
        return VIDEO

    monkeypatch.setattr(youtube, "_upload", upload)
    assert youtube.upload_run(conn, tmp_path) == 1  # the run's post step isn't failed by the quota
    conn.execute("DELETE FROM uploads")
    sent.clear()
    monkeypatch.setattr(
        youtube, "_upload", lambda *a: (_ for _ in ()).throw(HttpError(httplib2.Response({"status": 403}), b"quotaExceeded"))
    )
    with pytest.raises(HttpError):  # nothing posted at all: the step shows as failed
        youtube.upload_run(conn, tmp_path)


FAKE = "fake"
LOGIN = {"refresh_token": FAKE, "client_id": FAKE, "client_secret": FAKE}


class Creds:
    def __init__(self, scopes):
        self.granted_scopes, self.scopes = None, scopes


def test_views_are_read_back_for_every_public_post(conn, tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(youtube, "TOKEN", token)
    monkeypatch.setattr(youtube, "_credentials", lambda: Creds(youtube.SCOPES))
    db.save_clips(conn, "v1", [{**CLIP, "idx": i, "start": float(i), "file": f"v1/clips/0{i}.mp4"} for i in range(1, 7)])
    ids = [r[0] for r in conn.execute("SELECT id FROM clips ORDER BY idx")]
    now = utcnow()
    db.set_upload(conn, ids[0], "youtube", "https://youtube.com/shorts/abcdefghijk", at=now - youtube.GAP)
    db.set_upload(conn, ids[1], "youtube", "https://www.youtube.com/watch?v=ABCDEFGHIJK&t=3", at=now - youtube.GAP)
    db.set_upload(conn, ids[2], "youtube", "https://youtu.be/abcdefghijk", at=now - youtube.GAP)  # the same video
    db.set_upload(conn, ids[3], "youtube", "", at=now - youtube.GAP)  # marked posted with no link: nothing to read
    db.set_upload(conn, ids[4], "youtube", "https://youtube.com/shorts/scheduledxx", at=now + youtube.GAP)  # not public yet
    db.set_upload(conn, ids[5], "tiktok", "https://youtube.com/shorts/zzzzzzzzzzz", at=now - youtube.GAP)  # not YouTube
    db.set_stats(conn, "youtube", {ids[1]: (99, 9)}, now - 2 * youtube.STATS_EVERY)  # read before it was taken down
    asked = []
    monkeypatch.setattr(youtube, "_stats", lambda creds, vids: asked.append(sorted(vids)) or {"abcdefghijk": (1234, 56)})
    assert youtube.refresh_stats(conn, now) == 1
    assert asked == [["ABCDEFGHIJK", "abcdefghijk"]]
    views = {c["id"]: c["uploads"].get("youtube", {}).get("views") for c in db.overview(conn)["clips"]}
    assert [views[i] for i in ids] == [1234, None, 1234, None, None, None]  # the taken-down video lost its old count
    assert youtube.refresh_stats(conn, now + youtube.STATS_EVERY / 2) == 0  # the run's next step doesn't read again
    assert len(asked) == 1


def test_views_wait_for_a_login_that_may_read_them(conn, tmp_path, monkeypatch):
    clip_id = conn.execute("SELECT id FROM clips").fetchone()[0]
    db.set_upload(conn, clip_id, "youtube", "https://youtube.com/shorts/abcdefghijk", at=utcnow() - youtube.GAP)
    monkeypatch.setattr(youtube, "TOKEN", tmp_path / "missing.json")
    assert youtube.refresh_stats(conn) == 0  # no login at all: nothing to do, and no error
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(youtube, "TOKEN", token)
    monkeypatch.setattr(youtube, "_credentials", lambda: Creds([youtube.UPLOAD]))  # a login from before views
    monkeypatch.setattr(youtube, "_stats", lambda *_: pytest.fail("asked YouTube without leave to read"))
    assert youtube.refresh_stats(conn) == 0


def test_stats_are_read_in_pages_and_hidden_likes_stay_unknown(monkeypatch):
    calls = []

    class Api:
        def videos(self):
            return self

        def list(self, part, id, maxResults):
            calls.append(id.split(","))
            return self

        def execute(self, num_retries):
            return {"items": [{"id": "a", "statistics": {"viewCount": "7"}}] if len(calls) == 1 else []}

    monkeypatch.setattr(youtube, "build", lambda *a, **k: Api())
    vids = [f"v{i}" for i in range(youtube.PAGE + 1)]
    assert youtube._stats("creds", vids) == {"a": (7, None)}  # type: ignore[arg-type]
    assert [len(c) for c in calls] == [youtube.PAGE, 1]


def test_an_old_login_keeps_its_own_scopes(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    info = {"token": "t", **LOGIN, "scopes": [youtube.UPLOAD]}
    token.write_text(json.dumps(info))
    monkeypatch.setattr(youtube, "TOKEN", token)
    monkeypatch.setattr(youtube.Credentials, "refresh", lambda self, request: None)
    assert youtube._credentials().scopes == [youtube.UPLOAD]  # asking a refresh for READ would break posting


# the user unticked reading at consent, or Google didn't say what it granted: either way, posting alone is saved
@pytest.mark.parametrize("granted", [[youtube.UPLOAD], None])
def test_login_saves_only_the_scopes_google_granted(tmp_path, monkeypatch, granted):
    class Flow:
        def run_local_server(self, **_):
            return youtube.Credentials(
                "t",
                refresh_token=FAKE,
                client_id=FAKE,
                client_secret=FAKE,
                scopes=youtube.SCOPES,
                granted_scopes=granted,
            )

    (tmp_path / "client.json").write_text("{}")
    monkeypatch.setattr(youtube, "CLIENT", tmp_path / "client.json")
    monkeypatch.setattr(youtube, "HOME", tmp_path)
    monkeypatch.setattr(youtube, "TOKEN", tmp_path / "token.json")
    monkeypatch.setattr(youtube.InstalledAppFlow, "from_client_secrets_file", lambda *_: Flow())
    youtube.login()
    assert json.loads((tmp_path / "token.json").read_text())["scopes"] == [youtube.UPLOAD]
