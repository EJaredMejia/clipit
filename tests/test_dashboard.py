import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from clipit import dashboard, db, youtube

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
    assert status == 200 and b'<script src="/app.js">' in page
    assert "default-src 'self'" in csp and "script-src" not in csp  # no inline scripts, so injected HTML can't run any
    assert call(base + "/app.js")[0] == 200
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


NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="needs Node.js (CI installs it)")
def test_review_deck_decide_and_undo(tmp_path):
    """Runs dashboard.js itself under Node (tests/dashboard_deck.js) with scripted verdict saves."""
    assert NODE  # the skipif above; pyright doesn't narrow through it
    harness = (Path(__file__).parent / "dashboard_deck.js").read_text(encoding="utf-8")
    prelude, scenarios = harness.split("// SCENARIOS\n")
    page = (Path(dashboard.__file__).parent / "dashboard.js").read_text(encoding="utf-8")
    (tmp_path / "deck.cjs").write_text(f"{prelude}\n{page}\n{scenarios}", encoding="utf-8")
    r = subprocess.run([NODE, str(tmp_path / "deck.cjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    for case in got.values():
        assert case["ordered"] and case["pending"] == 0  # one verdict write at a time, none left marked pending

    # Keep, Undo, Keep again with the first save failing late: the later Keep and its Undo entry survive
    assert got["keepUndoKeep"] == {**got["keepUndoKeep"], "sent": ["keep", None, "keep"], "history": [1], "queue": [2]}
    # a lone failed Keep: card back on top, nothing to undo
    assert got["keepFails"]["history"] == [] and got["keepFails"]["queue"] == [1, 2]
    # Undo's save fails: the Keep stays decided and undoable
    assert got["undoFails"]["sent"] == ["keep", None] and got["undoFails"]["history"] == [1] and got["undoFails"]["queue"] == [2]
    # quick Keep then Undo: saved in that order, card back on top
    assert got["keepUndo"]["sent"] == ["keep", None] and got["keepUndo"]["history"] == [] and got["keepUndo"]["queue"] == [1, 2]


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
