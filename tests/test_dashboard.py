import json
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from clipit import dashboard, db, youtube
from clipit.util import utcnow

Site = tuple[str, sqlite3.Connection, list[int]]  # what the site fixture yields: base url, db, clip ids

CLIP = {"idx": 1, "file": "v1/clips/01_a.mp4", "start": 1.0, "end": 20.0, "title": "<img src=x onerror=alert(1)>",
        "hook": "h", "caption": "c", "youtube": "y", "streamer": "s", "trend": "", "sound": "", "score": 8.0, "why": ""}  # fmt: skip
VIDEO = bytes(range(256)) * 20


@pytest.fixture
def site(tmp_path):
    out = tmp_path / "out"
    (out / "v1" / "clips").mkdir(parents=True)
    (out / "v1" / "clips" / "01_a.mp4").write_bytes(VIDEO)
    (tmp_path / "secret.txt").write_text("not a clip")
    conn = db.connect(out / "clipit.db")
    db.record_manual(conn, "v1", "https://www.twitch.tv/videos/1")
    db.save_clips(conn, "v1", [CLIP, {**CLIP, "idx": 2, "start": 30.0, "end": 50.0, "file": "v1/../../secret.txt"}])
    ids = [r[0] for r in conn.execute("SELECT id FROM clips ORDER BY start_s")]
    server = dashboard.make_server(out, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", conn, ids
    server.shutdown()
    server.server_close()
    conn.close()


def call(url, body=None, **headers):
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_page_and_data(site):
    base, _, ids = site
    status, headers, page = call(base + "/")
    csp = headers["Content-Security-Policy"]
    assert status == 200 and b'src="/assets/app.js"' in page and b"<script>" not in page
    assert "default-src 'self'" in csp and "script-src" not in csp  # no inline scripts, so injected HTML can't run any
    for asset, ctype in (("/assets/app.js", "text/javascript"), ("/assets/app.css", "text/css")):
        status, headers, _ = call(base + asset)
        assert status == 200 and headers["Content-Type"].startswith(ctype)  # nosniff: a wrong type would block it
    status, headers, body = call(base + "/api/data")
    clips = json.loads(body)["clips"]
    assert status == 200 and [c["id"] for c in clips] == ids and clips[0]["title"] == CLIP["title"]  # sent as data
    assert call(base + "/api/data", **{"If-None-Match": headers["ETag"]})[0] == 304  # polling: nothing changed


def test_verdict_and_upload_are_saved(site):
    base, conn, (cid, _) = site
    assert call(base + "/api/verdict", {"clip": cid, "verdict": "keep"})[0] == 200
    assert call(base + "/api/upload", {"clip": cid, "platform": "tiktok", "url": "https://www.tiktok.com/@me/video/1"})[0] == 200
    (clip, _) = db.overview(conn)["clips"]
    assert clip["verdict"] == "keep" and clip["uploads"]["tiktok"]["url"] == "https://www.tiktok.com/@me/video/1"
    assert call(base + "/api/upload", {"clip": cid, "platform": "tiktok", "url": None})[0] == 200
    assert db.overview(conn)["clips"][0]["uploads"] == {}
    assert call(base + "/api/verdict", {"clip": 999, "verdict": "skip"})[0] == 404


@pytest.mark.parametrize(
    "body",
    [
        {"clip": 1, "verdict": "maybe"},
        {"clip": "1", "verdict": "keep"},
        {"clip": True, "verdict": "keep"},
        {"clip": 1, "platform": "myspace", "url": ""},
        {"clip": 1, "platform": "tiktok", "url": "javascript:alert(1)"},
        {"clip": 1, "platform": "tiktok", "url": "https://x.example/" + "a" * 600},
        ["not", "an", "object"],
    ],
)
def test_bad_writes_are_rejected(site, body):
    base, conn, _ = site
    path = "/api/upload" if isinstance(body, dict) and "platform" in body else "/api/verdict"
    assert call(base + path, body)[0] == 400
    assert all(c["verdict"] is None and not c["uploads"] for c in db.overview(conn)["clips"])


def test_other_sites_cannot_use_it(site):
    base, conn, (cid, _) = site
    assert call(base + "/api/data", Host="evil.example")[0] == 403  # DNS rebinding
    assert call(base + "/api/verdict", {"clip": cid, "verdict": "skip"}, Origin="https://evil.example")[0] == 403
    form = {"Content-Type": "text/plain"}  # what a cross-site form can send without a CORS preflight
    assert call(base + "/api/verdict", {"clip": cid, "verdict": "skip"}, **form)[0] == 400
    assert db.overview(conn)["clips"][0]["verdict"] is None


def test_video_ranges(site):
    base, _, (cid, outside) = site
    status, headers, body = call(f"{base}/media/{cid}", Range="bytes=10-19")
    assert status == 206 and body == VIDEO[10:20] and headers["Content-Range"] == f"bytes 10-19/{len(VIDEO)}"
    assert call(f"{base}/media/{cid}", Range="bytes=-6")[2] == VIDEO[-6:]
    assert call(f"{base}/media/{cid}")[2] == VIDEO
    assert call(f"{base}/media/{cid}", Range=f"bytes={len(VIDEO)}-")[0] == 416
    assert call(f"{base}/media/{outside}")[0] == 404  # a clip path pointing outside out/ is never served
    assert call(f"{base}/media/999")[0] == 404


def test_reveal_opens_only_real_clip_files(site, monkeypatch):
    base, _, (cid, outside) = site
    opened = []
    monkeypatch.setattr(dashboard, "reveal", opened.append)
    assert call(base + "/api/reveal", {"clip": cid})[0] == 200
    assert opened and opened[0].name == "01_a.mp4" and opened[0].is_file()
    assert call(base + "/api/reveal", {"clip": outside})[0] == 404  # a db path pointing outside out/ is never opened
    assert call(base + "/api/reveal", {"clip": 999})[0] == 404
    assert call(base + "/api/reveal", {"clip": cid}, Origin="https://evil.example")[0] == 403
    assert len(opened) == 1


def test_reveal_selects_the_file_in_explorer(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(dashboard.sys, "platform", "win32")
    monkeypatch.setattr(dashboard.subprocess, "run", lambda cmd, **kw: ran.append((cmd, kw)))
    dashboard.reveal(tmp_path / "a b.mp4")
    assert ran == [(f'explorer /select,"{tmp_path / "a b.mp4"}"', {"timeout": dashboard.REVEAL_TIMEOUT, "check": False})]


def test_reveal_failures_are_errors_except_explorers_exit_code(site, monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(dashboard.subprocess, "run", lambda cmd, **kw: ran.append(kw["check"]))
    for platform, check in (("linux", True), ("darwin", True), ("win32", False)):
        monkeypatch.setattr(dashboard.sys, "platform", platform)
        dashboard.reveal(tmp_path / "a.mp4")
        assert ran.pop() is check

    def fail(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(dashboard.subprocess, "run", fail)
    base, _, (cid, _) = site
    assert call(base + "/api/reveal", {"clip": cid})[0] == 500  # the page shows an error, not "Opened"


def test_post_to_youtube_from_the_page(site, monkeypatch, tmp_path):
    base, conn, (cid, outside) = site
    monkeypatch.setattr(youtube, "TOKEN", tmp_path / "missing.json")
    status, _, body = call(base + "/api/youtube", {"clip": cid})
    assert status == 500 and b"youtube-login" in body  # the page tells the user how to fix it
    sent = []
    monkeypatch.setattr(youtube, "_credentials", lambda: "creds")
    posted = {"id": "abc", "snippet": {"channelTitle": "ch"}, "status": {"privacyStatus": "public"}}
    monkeypatch.setattr(youtube, "_upload", lambda creds, video, body: sent.append(video) or posted)
    status, _, body = call(base + "/api/youtube", {"clip": cid})
    assert status == 200 and json.loads(body) == {"url": "https://youtube.com/shorts/abc"}
    assert db.overview(conn)["clips"][0]["uploads"]["youtube"]["status"] == "done"
    assert call(base + "/api/youtube", {"clip": cid})[0] == 409  # never posted twice
    assert call(base + "/api/youtube", {"clip": 999})[0] == 409
    status, _, body = call(base + "/api/youtube", {"clip": outside})
    assert status == 500 and b"outside" in body  # a db path pointing outside out/ is never sent
    assert call(base + "/api/youtube", {"clip": outside}, Origin="https://evil.example")[0] == 403
    assert [p.name for p in sent] == ["01_a.mp4"]


def test_post_text_edits(site):
    base, conn, (cid, _) = site
    assert call(base + "/api/text", {"clip": cid, "field": "youtube", "text": "é" * 5000})[0] == 200  # a full description
    assert call(base + "/api/text", {"clip": cid, "field": "title", "text": "New <title>"})[0] == 200
    clip = db.overview(conn)["clips"][0]
    assert clip["title"] == "New <title>" and clip["youtube"] == "é" * 5000
    for bad in (
        {"field": "title", "text": "   "},
        {"field": "title", "text": "x" * 101},
        {"field": "file", "text": "../secret.txt"},
        {"field": "caption", "text": 5},
    ):
        assert call(base + "/api/text", {"clip": cid, **bad})[0] == 400
    assert call(base + "/api/text", {"clip": 999, "field": "title", "text": "t"})[0] == 404
    assert call(base + "/api/text", {"clip": cid, "field": "title", "text": "t"}, Origin="https://evil.example")[0] == 403
    assert db.overview(conn)["clips"][0]["title"] == "New <title>"


def test_delete_removes_clip_and_video_only_inside_out(site: Site, tmp_path: Path) -> None:
    base, conn, (cid, outside) = site
    video = tmp_path / "out" / "v1" / "clips" / "01_a.mp4"
    db.claim_clip_upload(conn, cid, "youtube")
    assert call(base + "/api/delete", {"clip": cid})[0] == 404  # mid-upload: refused
    assert video.exists()
    db.set_upload(conn, cid, "youtube", "https://youtu.be/x")
    assert call(base + "/api/delete", {"clip": cid})[0] == 200
    assert not video.exists() and db.clip_file(conn, cid) is None
    assert conn.execute("SELECT count(*) FROM uploads").fetchone()[0] == 0
    assert call(base + "/api/delete", {"clip": outside})[0] == 200
    assert (tmp_path / "secret.txt").exists()  # its db path points outside out/: the row goes, the file stays


def test_delete_is_all_or_nothing_when_the_video_is_locked(site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, conn, (cid, other) = site
    clock = [0.0]

    def locked(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("in use")  # what Windows says while another program has the file open

    def sleep(s: float) -> None:
        clock[0] += s

    monkeypatch.setattr(dashboard.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dashboard.time, "sleep", sleep)
    monkeypatch.setattr(Path, "unlink", locked)
    status, _, body = call(base + "/api/delete", {"clip": cid})
    assert status == 409 and b"open in another program" in body
    assert db.clip_file(conn, cid) and (tmp_path / "out" / "v1" / "clips" / "01_a.mp4").exists()  # row and file both stay
    assert clock[0] >= dashboard.DELETE_WAIT  # it waited for the lock to go before giving up
    (tmp_path / "out" / "v1" / "clips" / "03_b.mp4").write_bytes(VIDEO)
    db.save_clips(conn, "v1", [CLIP, {**CLIP, "start": 30.0, "end": 50.0, "file": "v1/../../secret.txt"},
                               {**CLIP, "start": 60.0, "end": 80.0, "file": "v1/clips/03_b.mp4"}])  # fmt: skip
    for (c,) in conn.execute("SELECT id FROM clips").fetchall():
        db.set_verdict(conn, c, "skip")
    clock[0] = 0.0
    status, _, body = call(base + "/api/delete-skipped", {"skipped": True})
    assert status == 200 and json.loads(body) == {"deleted": 1, "failed": 2}  # only the one outside out/ had nothing to unlink
    assert clock[0] < 2 * dashboard.DELETE_WAIT  # one wait for the whole batch, not one per locked video
    assert other not in [c["id"] for c in db.overview(conn)["clips"]]


def test_delete_skipped_spares_a_clip_kept_since(site: Site) -> None:
    _, conn, (cid, _) = site
    with db.deleting_clip(conn, cid, only_skipped=True) as file:  # another tab undid the skip after the list was read
        assert file is None
    assert db.clip_file(conn, cid)


def test_delete_skipped(site: Site, tmp_path: Path) -> None:
    base, conn, (cid, other) = site
    db.set_verdict(conn, cid, "skip")
    status, _, body = call(base + "/api/delete-skipped", {"skipped": True})
    assert status == 200 and json.loads(body) == {"deleted": 1, "failed": 0}
    assert not (tmp_path / "out" / "v1" / "clips" / "01_a.mp4").exists()
    assert [c["id"] for c in db.overview(conn)["clips"]] == [other]
    db.set_verdict(conn, other, "skip")
    for body, ctype in [(b"{}", "application/json"), (b"[]", "application/json"), (b"{bad", "application/json"),
                        (b"", "application/json"), (b'{"skipped": true}', "text/plain")]:  # fmt: skip
        req = urllib.request.Request(base + "/api/delete-skipped", body, {"Content-Type": ctype})
        with pytest.raises(urllib.error.HTTPError):  # empty, broken or non-JSON requests delete nothing
            urllib.request.urlopen(req, timeout=10)
    assert db.clip_file(conn, other)


def test_delete_vod_keeps_its_clips_and_deletes_everything_else(site: Site, tmp_path: Path) -> None:
    base, conn, (cid, other) = site
    out = tmp_path / "out"
    (out / "v1" / "chat.json").write_text("[]")
    (out / "v1" / "transcripts").mkdir()
    (out / "v1" / "transcripts" / "1.json").write_text("{}")
    (out / "v1" / "clips" / "02_dropped.mp4").write_bytes(VIDEO)  # a render the catalog no longer has
    (out / "v1" / "clips" / "01_a.ass").write_text("")  # subtitles left by a render
    db.set_upload(conn, cid, "youtube", "https://youtu.be/x")
    assert call(base + "/api/delete-vod", {"vod": "../v1"})[0] == 400  # not a folder name: refused
    assert call(base + "/api/delete-vod", {"vod": "v1"})[0] == 200
    assert [p.relative_to(out).as_posix() for p in sorted((out / "v1").rglob("*"))] == [
        "v1/clips",
        "v1/clips/01_a.mp4",
        "v1/clips/02_dropped.mp4",
    ]
    assert (tmp_path / "secret.txt").exists()  # a clip's db path pointing outside out/ is never walked into
    data = db.overview(conn)
    assert data["vods"] == [] and {c["id"] for c in data["clips"]} == {cid, other}  # the clips stay, the VOD is hidden
    assert data["clips"][0]["uploads"]["youtube"]["url"] == "https://youtu.be/x"
    assert not db.claim(conn, "v1", "https://www.twitch.tv/videos/1", "s", "top")  # polling never clips it again
    assert call(base + "/api/delete-vod", {"vod": "v1"})[0] == 404
    db.record_manual(conn, "v1", "https://www.twitch.tv/videos/1")  # the user ran it again by hand: it's back
    assert [v["id"] for v in db.overview(conn)["vods"]] == ["v1"]


def test_delete_vod_stays_when_a_file_is_open(site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base, conn, (cid, _) = site
    (tmp_path / "out" / "v1" / "chat.json").write_text("[]")
    clock = [0.0]

    def locked(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("in use")

    def sleep(s: float) -> None:
        clock[0] += s

    monkeypatch.setattr(dashboard.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dashboard.time, "sleep", sleep)
    monkeypatch.setattr(Path, "unlink", locked)
    status, _, body = call(base + "/api/delete-vod", {"vod": "v1"})
    assert status == 409 and b"open in another program" in body
    assert [v["id"] for v in db.overview(conn)["vods"]] == ["v1"]  # still listed, so it can be retried
    assert clock[0] >= dashboard.DELETE_WAIT


def test_delete_vod_refuses_one_being_processed(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "clipit.db")
    assert db.claim(conn, "v2", "u", "s", "top")
    with db.deleting_vod(conn, "v2") as clips:
        assert clips is None
    with db.deleting_vod(conn, "v2", now=utcnow() + db.STALE_CLAIM * 2) as clips:  # a crashed run's stale claim
        assert clips == []


def test_delete_vod_refuses_one_a_manual_run_is_using(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "clipit.db")
    db.record_manual(conn, "v3", "u")
    with db.running(conn, "v3"):  # clipit <url> rerunning a done VOD: its status stays 'done'
        assert db.status(conn, "v3") == "done"
        with db.deleting_vod(conn, "v3") as clips:
            assert clips is None
        with db.deleting_vod(conn, "v3", now=utcnow() + db.STALE_CLAIM * 2) as clips:  # it crashed long ago
            assert clips == []
            conn.execute("UPDATE vods SET deleted = 0")  # undo, to check the lease is let go below
    with db.deleting_vod(conn, "v3") as clips:
        assert clips == []
