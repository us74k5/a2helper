"""The page: a small web server on this computer only (127.0.0.1).

Every API request must carry a token that only the page itself knows, and the Host header
must be 127.0.0.1 or localhost, so a website you have open cannot drive it.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_NAME

FROZEN = bool(getattr(sys, "frozen", False))
WEB_DIR = Path(getattr(sys, "_MEIPASS", "")) / "web" if FROZEN else Path(__file__).resolve().parent / "web"
PAGE_MARKER = b"<!-- aion2-helper page -->"


class BadRequest(ValueError):
    pass


def make_handler(api, token: str, port_box: list):
    class Handler(BaseHTTPRequestHandler):
        server_version = "aion2-helper"

        def log_message(self, *a):  # keep the terminal quiet
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in {f"127.0.0.1:{port_box[0]}", f"localhost:{port_box[0]}"}

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode())

        def _guard(self) -> bool:
            if not self._host_ok():
                self._json(403, {"error": "bad host"})
                return False
            if not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                self._json(403, {"error": "bad token"})
                return False
            return True

        def _run(self, fn):
            try:
                self._json(200, fn())
            except (BadRequest, KeyError, TypeError, ValueError) as e:
                self._json(400, {"error": str(e) if not isinstance(e, KeyError) else f"bad request: {e}"})
            except RuntimeError as e:
                self._json(409, {"error": str(e)})
            except OSError as e:
                self._json(500, {"error": str(e)})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                if not self._host_ok():
                    return self._json(403, {"error": "bad host"})
                html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if path == "/api/state":
                if self._guard():
                    self._run(api.state)
                return
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self.path.startswith("/api/") or not self._guard():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"error": "body is not a JSON object"})
            self._run(lambda: api.post(self.path, body))

    return Handler


def is_our_page(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.0) as r:
            return PAGE_MARKER in r.read(4096)
    except OSError:
        return False


def open_url(url: str) -> None:
    """Open the page in the default browser, without a packaged build's libraries leaking into it."""
    saved = dict(os.environ)
    try:
        if "LD_LIBRARY_PATH_ORIG" in os.environ:
            os.environ["LD_LIBRARY_PATH"] = os.environ.pop("LD_LIBRARY_PATH_ORIG")
        elif FROZEN:
            os.environ.pop("LD_LIBRARY_PATH", None)
        webbrowser.open(url)
    except Exception as e:  # the page is still reachable by hand
        print(f"could not open a browser ({e}); open {url} yourself", flush=True)
    finally:
        os.environ.clear()
        os.environ.update(saved)


class WebServer:
    def __init__(self, api, port: int = 8766):
        self.token = secrets.token_urlsafe(24)
        self.port_box = [port]
        self.server = None
        for p in range(port, port + 20):
            try:
                self.port_box[0] = p
                self.server = ThreadingHTTPServer(("127.0.0.1", p), make_handler(api, self.token, self.port_box))
                break
            except OSError:
                continue
        if self.server is None:
            raise SystemExit(f"{APP_NAME}: no free port between {port} and {port + 19}")
        self.server.daemon_threads = True

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port_box[0]}/"

    def start(self) -> None:
        threading.Thread(target=self.server.serve_forever, name="web", daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
