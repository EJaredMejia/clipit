import json
import threading
import urllib.error
import urllib.request

import pytest

from clipit import dashboard, db

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
