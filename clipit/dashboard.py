"""The local dashboard: every VOD and clip, playable, with your keep/skip call and where each clip was posted.

A standard-library HTTP server on 127.0.0.1. The page polls /api/data and saves through /api/verdict and /api/upload;
every database read and write goes through clipit.db. /api/reveal opens a clip's folder in the file manager.

Security: other websites open in the same browser can send requests to localhost. So every request must carry our own
Host (stops DNS rebinding), and writes must be JSON from our own Origin (a cross-site JSON POST needs a CORS
preflight, which is never answered). The page treats every field as text: titles and AI output come from strangers.
"""

import contextlib
import functools
import hashlib
import http.server
import json
import re
import sqlite3
import subprocess
import sys
import webbrowser
from pathlib import Path

from clipit import db
from clipit.util import ClipitError, log, logger

STATIC = {"/": ("dashboard.html", "text/html"), "/app.js": ("dashboard.js", "text/javascript")}
CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
CHUNK = 1 << 20  # video bytes per range response: short responses keep a paused player from holding a thread open
MAX_BODY = 4096
MAX_URL = 500
REVEAL_TIMEOUT = 15


def make_server(out: Path, port: int) -> http.server.ThreadingHTTPServer:
    db.connect(out / "clipit.db").close()  # create or upgrade it once, before requests race to
    try:
        return http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, out.resolve()))
    except OSError as e:
        raise ClipitError(f"can't start the dashboard on port {port}: {e}") from e


def serve(out: Path, port: int) -> None:
    server = make_server(out, port)
    url = f"http://127.0.0.1:{server.server_port}/"
    log(f"dashboard at {url} (Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class Handler(http.server.BaseHTTPRequestHandler):
    timeout = 30  # a stalled browser connection frees its thread instead of holding it forever

    def __init__(self, out: Path, *args) -> None:
        self.out = out  # set before super().__init__, which handles the request
        super().__init__(*args)

    def log_message(self, format, *args) -> None:
        logger.debug("dashboard: " + format, *args)

    def do_GET(self) -> None:
        if not self.ours():
            return
        path = self.path.split("?", 1)[0]
        if path in STATIC:
            name, ctype = STATIC[path]
            body = (Path(__file__).parent / name).read_bytes()
            return self.send(200, body, f"{ctype}; charset=utf-8", {"Content-Security-Policy": CSP})
        if path == "/api/data":
            with self.conn() as conn:
                body = json.dumps(db.overview(conn)).encode()
            etag = '"' + hashlib.sha1(body, usedforsecurity=False).hexdigest() + '"'
            if self.headers.get("If-None-Match") == etag:  # the page polls: only send what changed
                return self.send(304, headers={"ETag": etag})
            return self.send(200, body, headers={"ETag": etag})
        if m := re.fullmatch(r"/media/(\d+)", path):
            return self.video(int(m[1]))
        self.send(404, b'{"error": "not found"}')

    def do_POST(self) -> None:
        if not self.ours():
            return
        req = self.read_json()
        clip = req.get("clip")
        if not isinstance(clip, int) or isinstance(clip, bool):
            return self.send(400, b'{"error": "expected JSON with a clip id"}')
        if self.path == "/api/verdict" and req.get("verdict") in ("keep", "skip", None):
            with self.conn() as conn:
                found = db.set_verdict(conn, clip, req.get("verdict"))
        elif self.path == "/api/upload" and req.get("platform") in db.PLATFORMS and valid_url(req.get("url")):
            with self.conn() as conn:
                found = db.set_upload(conn, clip, req["platform"], req.get("url"))
        elif self.path == "/api/reveal":
            if not (path := self.clip_path(clip)):
                return self.send(404, b'{"error": "no video"}')
            try:
                reveal(path)
            except (OSError, subprocess.SubprocessError) as e:  # not found, timed out, or exited with an error
                logger.warning("couldn't open the folder of %s: %s", path, e)
                return self.send(500, b'{"error": "could not open the folder"}')
            found = True
        else:
            return self.send(400, b'{"error": "bad request"}')
        self.send(200 if found else 404, b'{"ok": true}' if found else b'{"error": "no such clip"}')

    def ours(self) -> bool:
        """Only requests from our own page: right Host always, right Origin when the browser sends one."""
        host = self.headers.get("Host", "")
        port = self.connection.getsockname()[1]
        origin = self.headers.get("Origin")
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}") or origin not in (None, f"http://{host}"):
            self.send(403, b'{"error": "forbidden"}')
            return False
        return True

    def read_json(self) -> dict:
        """The request's JSON object, {} when it isn't one."""
        if self.headers.get_content_type() != "application/json":
            return {}
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if 0 < n <= MAX_BODY else None
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def conn(self) -> contextlib.closing[sqlite3.Connection]:
        return contextlib.closing(db.connect(self.out / "clipit.db"))  # one per request: connections can't cross threads

    def clip_path(self, clip_id: int) -> Path | None:
        """The clip's video file, None when it's missing or its db path points outside out/."""
        with self.conn() as conn:
            file = db.clip_file(conn, clip_id)
        path = (self.out / file).resolve() if file else None
        return path if path and path.is_relative_to(self.out) and path.is_file() else None

    def video(self, clip_id: int) -> None:
        if not (path := self.clip_path(clip_id)):
            return self.send(404, b'{"error": "no video"}')
        size = path.stat().st_size
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if not m or not (m[1] or m[2]):
            start, end, code = 0, size - 1, 200
        elif m[1]:
            start, end, code = int(m[1]), min(int(m[2] or size - 1), size - 1), 206
        else:  # suffix range: the last N bytes
            start, end, code = max(0, size - int(m[2])), size - 1, 206
        if start > end:
            return self.send(416, headers={"Content-Range": f"bytes */{size}"})
        if code == 206:
            end = min(end, start + CHUNK - 1)
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
        if code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left and (data := f.read(min(left, 64 * 1024))):
                    self.wfile.write(data)
                    left -= len(data)
        except (ConnectionError, TimeoutError):
            pass  # the player cancels requests all the time when seeking

    def send(self, code: int, body: bytes = b"", ctype: str = "application/json", headers: dict | None = None) -> None:
        self.send_response(code)
        headers = {"Content-Type": ctype, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", **(headers or {})}
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def reveal(path: Path) -> None:
    """Open the file manager on this PC with the file selected, ready to drag into an upload page."""
    if sys.platform == "win32":
        # a string, not a list: explorer only parses /select,"path" with the quotes after the comma (paths can't hold ")
        cmd: str | list[str] = f'explorer /select,"{path}"'
    elif sys.platform == "darwin":
        cmd = ["open", "-R", str(path)]
    else:
        cmd = ["xdg-open", str(path.parent)]  # no portable "select this file" on Linux: open its folder
    # explorer exits 1 even when it worked, so only its exit code is ignored; open / xdg-open failures are errors
    subprocess.run(cmd, timeout=REVEAL_TIMEOUT, check=sys.platform != "win32")


def valid_url(url: object) -> bool:
    """A post link to show as a link: None (un-mark), '' (posted, no link) or a plain http(s) URL."""
    return url is None or url == "" or (isinstance(url, str) and len(url) <= MAX_URL and bool(re.fullmatch(r"https?://\S+", url)))
