"""The local HTTP + WebSocket server behind the village view.

Security posture — this matters more than usual, because the data this server can
reach is the user's private source code, prompts and tool output:

* It binds **127.0.0.1 only**, never 0.0.0.0.
* Every request must carry a **token** generated at startup and written to a
  0600 file in the XDG runtime dir. Any local process can open a TCP port on
  loopback, so the port alone is not an authorization boundary.
* The token is compared with :func:`hmac.compare_digest` to avoid leaking it a
  byte at a time through timing.
* Static file serving is rooted at the bundled ``web/`` directory and resolves
  symlinks before checking containment, so ``..`` and symlink escapes cannot
  read arbitrary files.
* ``Access-Control-Allow-Origin`` is never sent, and WebSocket upgrades are
  rejected unless the ``Origin`` header is absent (a native webview) or points
  at our own address — otherwise any web page you visit could connect.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .wsproto import Broadcaster, WebSocketConnection, handshake_response

#: Bytes of entropy in the session token.
_TOKEN_BYTES = 32

_SAFE_TEXT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def runtime_dir() -> str:
    """Where the port/token handshake file lives."""
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache")
    path = os.path.join(base, "codeville")
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def endpoint_file() -> str:
    return os.path.join(runtime_dir(), "endpoint.json")


def write_endpoint(host: str, port: int, token: str) -> str:
    """Publish the connection details for the tray and any browser launcher."""
    path = endpoint_file()
    payload = {"host": host, "port": port, "token": token,
               "url": f"http://{host}:{port}/?token={token}"}
    tmp = path + ".tmp"
    # Create with 0600 from the start: never let the token exist world-readable.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)
    return path


def read_endpoint() -> Optional[dict]:
    try:
        with open(endpoint_file()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("port") else None


def clear_endpoint() -> None:
    try:
        os.remove(endpoint_file())
    except OSError:
        pass


class _QuietHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not dump a traceback when a tab closes.

    Browsers and webviews drop long-lived connections all the time; the stdlib
    default prints the whole stack to stderr, which looks like a crash in the
    daemon log. Genuine errors are still reported.
    """

    daemon_threads = True
    #: The UI holds a long-lived WebSocket per view; leave room for a few.
    request_queue_size = 32
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class CodevilleServer:
    """HTTP + WebSocket server serving the village UI and its event stream."""

    def __init__(self, web_root: str, host: str = "127.0.0.1", port: int = 0,
                 token: Optional[str] = None,
                 state_provider: Optional[Callable[[], dict]] = None,
                 on_client_message: Optional[Callable[[dict], None]] = None) -> None:
        self.web_root = os.path.realpath(web_root)
        self.host = host
        self.requested_port = port
        self.token = token or secrets.token_urlsafe(_TOKEN_BYTES)
        self.broadcaster = Broadcaster()
        self._state_provider = state_provider or (lambda: {})
        self._on_client_message = on_client_message
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else 0

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?token={self.token}"

    def start(self) -> "CodevilleServer":
        handler = _make_handler(self)
        self._httpd = _QuietHTTPServer((self.host, self.requested_port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.2},
                                        daemon=True, name="codeville-http")
        self._thread.start()
        write_endpoint(self.host, self.port, self.token)
        return self

    def stop(self) -> None:
        for conn in self.broadcaster.snapshot():
            conn.close(1001, "server shutting down")
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        clear_endpoint()

    # -- outbound ----------------------------------------------------------

    def broadcast(self, event: dict) -> int:
        """Push one event to every connected view."""
        return self.broadcaster.broadcast_text(json.dumps(event, default=str))

    @property
    def client_count(self) -> int:
        return len(self.broadcaster)

    # -- auth --------------------------------------------------------------

    def check_token(self, candidate: Optional[str]) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(candidate, self.token)

    def origin_allowed(self, origin: Optional[str]) -> bool:
        """Reject cross-origin WebSocket upgrades.

        A native webview sends no Origin. A browser tab opened on our own URL
        sends exactly our address. Anything else is another site trying to reach
        the daemon through the user's browser.
        """
        if not origin:
            return True
        allowed = {f"http://{self.host}:{self.port}", f"http://localhost:{self.port}"}
        return origin.rstrip("/") in allowed

    # -- static ------------------------------------------------------------

    def resolve_static(self, url_path: str) -> Optional[str]:
        """Map a URL path to a file inside web_root, or None if it escapes."""
        relative = unquote(urlparse(url_path).path).lstrip("/")
        if not relative or relative.endswith("/"):
            relative += "index.html"
        candidate = os.path.realpath(os.path.join(self.web_root, relative))
        root = self.web_root + os.sep
        if not (candidate == self.web_root or candidate.startswith(root)):
            return None  # traversal or symlink escape
        return candidate if os.path.isfile(candidate) else None

    def current_state(self) -> dict:
        try:
            return self._state_provider() or {}
        except Exception as exc:  # a state bug must not take the server down
            return {"error": f"state unavailable: {exc}"}

    def handle_client_message(self, payload: dict) -> None:
        if self._on_client_message is not None:
            try:
                self._on_client_message(payload)
            except Exception:
                pass


def _make_handler(server: CodevilleServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Codeville"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # -- helpers ------------------------------------------------------

        def log_message(self, fmt, *args):  # noqa: A003 - silence stderr spam
            pass

        def _token_from_request(self) -> Optional[str]:
            header = self.headers.get("X-Codeville-Token")
            if header:
                return header.strip()
            query = parse_qs(urlparse(self.path).query)
            values = query.get("token")
            return values[0] if values else None

        def _send(self, code: int, body: bytes, content_type: str,
                  extra: Optional[dict] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # This data is private; never let a proxy or the browser keep it.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload, default=str).encode(),
                       "application/json; charset=utf-8")

        # -- routing ------------------------------------------------------

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path

            if path == "/ws":
                self._handle_websocket()
                return

            # The shell page is served without a token so the browser can load it
            # and then authenticate; it contains no private data itself.
            if path in ("/", "/index.html"):
                self._serve_file("/index.html")
                return

            if path == "/api/health":
                self._json(200, {"ok": True, "clients": server.client_count})
                return

            if path == "/api/state":
                if not server.check_token(self._token_from_request()):
                    self._json(401, {"error": "bad or missing token"})
                    return
                self._json(200, server.current_state())
                return

            self._serve_file(path)

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def _serve_file(self, path: str) -> None:
            resolved = server.resolve_static(path)
            if resolved is None:
                self._json(404, {"error": "not found"})
                return
            try:
                with open(resolved, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            ext = os.path.splitext(resolved)[1].lower()
            ctype = _SAFE_TEXT_TYPES.get(ext) or mimetypes.guess_type(resolved)[0] \
                or "application/octet-stream"
            self._send(200, body, ctype)

        # -- websocket ----------------------------------------------------

        def _handle_websocket(self) -> None:
            if not server.check_token(self._token_from_request()):
                self._json(401, {"error": "bad or missing token"})
                return
            if not server.origin_allowed(self.headers.get("Origin")):
                self._json(403, {"error": "origin not allowed"})
                return
            key = self.headers.get("Sec-WebSocket-Key")
            upgrade = (self.headers.get("Upgrade") or "").lower()
            if not key or upgrade != "websocket":
                self._json(400, {"error": "expected a websocket upgrade"})
                return

            self.wfile.write(handshake_response(key))
            self.wfile.flush()

            conn = WebSocketConnection(self.connection, self.client_address)
            server.broadcaster.add(conn)
            try:
                conn.send_text(json.dumps({"t": "hello", **server.current_state()},
                                          default=str))
                conn.read_loop(_on_text)
            finally:
                server.broadcaster.discard(conn)
                # Tell http.server not to touch this hijacked socket again.
                self.close_connection = True

    def _on_text(_conn, text: str) -> None:
        try:
            payload = json.loads(text)
        except ValueError:
            return
        if isinstance(payload, dict):
            server.handle_client_message(payload)

    return Handler


def find_free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for an unused port (used when a fixed port is wanted up front)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]
