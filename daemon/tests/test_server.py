"""Tests for the local HTTP + WebSocket server, with the security rules pinned.

The daemon can reach the user's private transcripts, so the auth, traversal and
origin checks are treated as load-bearing behaviour, not nice-to-haves.
"""

import base64
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from codeville.server import (  # noqa: E402
    CodevilleServer,
    find_free_port,
    read_endpoint,
    write_endpoint,
)
from codeville.wsproto import OP_TEXT, FrameDecoder  # noqa: E402


def http_get(url, token=None, method="GET"):
    request = urllib.request.Request(url, method=method)
    if token:
        request.add_header("X-Codeville-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read(), dict(err.headers)


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="codeville-srv-")
        self.web = os.path.join(self.tmp, "web")
        os.makedirs(os.path.join(self.web, "js"))
        with open(os.path.join(self.web, "index.html"), "w") as fh:
            fh.write("<h1>Codeville</h1>")
        with open(os.path.join(self.web, "js", "app.js"), "w") as fh:
            fh.write("console.log('hi')")
        # A secret that must never be reachable through the static route.
        self.secret = os.path.join(self.tmp, "secret.txt")
        with open(self.secret, "w") as fh:
            fh.write("TOP SECRET")

        self.state = {"villages": [], "n": 1}
        self.received = []
        self.server = CodevilleServer(
            web_root=self.web,
            state_provider=lambda: self.state,
            on_client_message=self.received.append,
        ).start()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestStatic(ServerTestCase):
    def test_index_served_without_token(self):
        status, body, _ = http_get(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Codeville", body)

    def test_nested_asset_served_with_correct_type(self):
        status, body, headers = http_get(self.base + "/js/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"console.log", body)
        self.assertTrue(headers["Content-Type"].startswith("text/javascript"))

    def test_missing_file_is_404(self):
        self.assertEqual(http_get(self.base + "/nope.js")[0], 404)

    def test_private_data_is_not_cacheable(self):
        _, _, headers = http_get(self.base + "/")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_no_cors_header_is_ever_sent(self):
        _, _, headers = http_get(self.base + "/")
        self.assertNotIn("Access-Control-Allow-Origin", headers)


class TestTraversal(ServerTestCase):
    def test_dotdot_escape_is_refused(self):
        for attack in ("/../secret.txt", "/js/../../secret.txt",
                       "/%2e%2e/secret.txt", "/....//secret.txt"):
            status, body, _ = http_get(self.base + attack)
            self.assertNotIn(b"TOP SECRET", body, attack)
            self.assertIn(status, (400, 404), attack)

    def test_absolute_path_is_refused(self):
        status, body, _ = http_get(self.base + "//etc/passwd")
        self.assertNotIn(b"root:", body)
        self.assertEqual(status, 404)

    def test_symlink_out_of_root_is_refused(self):
        link = os.path.join(self.web, "leak.txt")
        os.symlink(self.secret, link)
        status, body, _ = http_get(self.base + "/leak.txt")
        self.assertNotIn(b"TOP SECRET", body)
        self.assertEqual(status, 404)

    def test_resolve_static_returns_none_for_escape(self):
        self.assertIsNone(self.server.resolve_static("/../secret.txt"))
        self.assertIsNotNone(self.server.resolve_static("/index.html"))


class TestAuth(ServerTestCase):
    def test_state_requires_token(self):
        self.assertEqual(http_get(self.base + "/api/state")[0], 401)

    def test_state_with_header_token(self):
        status, body, _ = http_get(self.base + "/api/state", token=self.server.token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["n"], 1)

    def test_state_with_query_token(self):
        status, body, _ = http_get(
            f"{self.base}/api/state?token={self.server.token}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["n"], 1)

    def test_wrong_token_rejected(self):
        self.assertEqual(http_get(self.base + "/api/state", token="nope")[0], 401)

    def test_token_is_long_and_random(self):
        self.assertGreaterEqual(len(self.server.token), 32)
        other = CodevilleServer(web_root=self.web)
        self.assertNotEqual(self.server.token, other.token)

    def test_health_needs_no_token(self):
        status, body, _ = http_get(self.base + "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_state_provider_exception_is_contained(self):
        def boom():
            raise RuntimeError("state exploded")

        self.server._state_provider = boom
        status, body, _ = http_get(self.base + "/api/state", token=self.server.token)
        self.assertEqual(status, 200)
        self.assertIn("state unavailable", json.loads(body)["error"])


class TestOriginPolicy(ServerTestCase):
    def test_native_webview_without_origin_allowed(self):
        self.assertTrue(self.server.origin_allowed(None))
        self.assertTrue(self.server.origin_allowed(""))

    def test_own_origin_allowed(self):
        self.assertTrue(self.server.origin_allowed(f"http://127.0.0.1:{self.server.port}"))
        self.assertTrue(self.server.origin_allowed(f"http://localhost:{self.server.port}"))

    def test_foreign_origin_refused(self):
        for bad in ("http://evil.example", "https://evil.example",
                    "http://127.0.0.1:1", "null"):
            self.assertFalse(self.server.origin_allowed(bad), bad)


def decode_server_frames(buf: bytearray):
    """Decode unmasked server->client text frames, returning (payloads, leftover).

    The library's FrameDecoder is deliberately server-side and rejects unmasked
    frames, so the test client needs this small counterpart.
    """
    out = []
    while len(buf) >= 2:
        opcode = buf[0] & 0x0F
        length = buf[1] & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < 4:
                break
            length = struct.unpack_from("!H", buf, 2)[0]
            offset = 4
        elif length == 127:
            if len(buf) < 10:
                break
            length = struct.unpack_from("!Q", buf, 2)[0]
            offset = 10
        if buf[1] & 0x80:  # a server must never mask
            raise AssertionError("server sent a masked frame")
        if len(buf) < offset + length:
            break
        payload = bytes(buf[offset:offset + length])
        del buf[:offset + length]
        if opcode == OP_TEXT:
            out.append(payload)
    return out


class WsClient:
    """A tiny WebSocket client so the tests exercise the real handshake."""

    def __init__(self, host, port, token, origin=None):
        self.sock = socket.create_connection((host, port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET /ws?token={token} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if origin:
            lines.append(f"Origin: {origin}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        self.status = int(head.split(b" ")[1]) if b" " in head else 0
        self.buf = bytearray()
        self.pending = []
        # A rejected upgrade replies with a JSON body, not frames.
        if self.status == 101 and rest:
            self.buf += rest
            self.pending = decode_server_frames(self.buf)

    def recv_json(self, timeout=5):
        deadline = time.time() + timeout
        while not self.pending and time.time() < deadline:
            self.sock.settimeout(0.2)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            self.buf += chunk
            self.pending += decode_server_frames(self.buf)
        return json.loads(self.pending.pop(0)) if self.pending else None

    def send_json(self, payload):
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        masked = bytearray(data)
        for i in range(len(masked)):
            masked[i] ^= mask[i & 3]
        n = len(data)
        if n < 126:
            header = struct.pack("!BB", 0x81, 0x80 | n)
        else:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        self.sock.sendall(header + mask + bytes(masked))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class TestWebSocket(ServerTestCase):
    def test_upgrade_requires_token(self):
        client = WsClient("127.0.0.1", self.server.port, "wrong-token")
        self.assertEqual(client.status, 401)
        client.close()

    def test_upgrade_refuses_foreign_origin(self):
        client = WsClient("127.0.0.1", self.server.port, self.server.token,
                          origin="http://evil.example")
        self.assertEqual(client.status, 403)
        client.close()

    def test_hello_carries_initial_state(self):
        client = WsClient("127.0.0.1", self.server.port, self.server.token)
        self.assertEqual(client.status, 101)
        hello = client.recv_json()
        self.assertEqual(hello["t"], "hello")
        self.assertEqual(hello["n"], 1)
        client.close()

    def test_broadcast_reaches_connected_client(self):
        client = WsClient("127.0.0.1", self.server.port, self.server.token)
        client.recv_json()  # hello

        deadline = time.time() + 3
        while self.server.client_count == 0 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.server.broadcast({"t": "agent.spawn", "id": "a1"}), 1)

        event = client.recv_json()
        self.assertEqual(event["t"], "agent.spawn")
        self.assertEqual(event["id"], "a1")
        client.close()

    def test_client_message_reaches_callback(self):
        client = WsClient("127.0.0.1", self.server.port, self.server.token)
        client.recv_json()
        client.send_json({"t": "focus", "village": "taha"})

        deadline = time.time() + 3
        while not self.received and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.received[0]["village"], "taha")
        client.close()

    def test_disconnect_is_pruned(self):
        client = WsClient("127.0.0.1", self.server.port, self.server.token)
        client.recv_json()
        deadline = time.time() + 3
        while self.server.client_count == 0 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.server.client_count, 1)

        client.close()
        deadline = time.time() + 5
        while self.server.client_count and time.time() < deadline:
            self.server.broadcast({"t": "ping"})
            time.sleep(0.05)
        self.assertEqual(self.server.client_count, 0)

    def test_two_clients_both_receive(self):
        a = WsClient("127.0.0.1", self.server.port, self.server.token)
        b = WsClient("127.0.0.1", self.server.port, self.server.token)
        a.recv_json(); b.recv_json()

        deadline = time.time() + 3
        while self.server.client_count < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.server.broadcast({"t": "tick"}), 2)
        self.assertEqual(a.recv_json()["t"], "tick")
        self.assertEqual(b.recv_json()["t"], "tick")
        a.close(); b.close()


class TestBinding(ServerTestCase):
    def test_binds_loopback_only(self):
        self.assertEqual(self.server.host, "127.0.0.1")
        # Connecting via a non-loopback local address must fail.
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip.startswith("127."):
            self.skipTest("no non-loopback address available")
        with self.assertRaises(OSError):
            socket.create_connection((host_ip, self.server.port), timeout=1)

    def test_find_free_port(self):
        self.assertGreater(find_free_port(), 0)


class TestEndpointFile(ServerTestCase):
    def test_endpoint_written_and_readable(self):
        data = read_endpoint()
        self.assertEqual(data["port"], self.server.port)
        self.assertEqual(data["token"], self.server.token)

    def test_endpoint_file_is_not_world_readable(self):
        write_endpoint("127.0.0.1", 1234, "secret-token")
        from codeville.server import endpoint_file
        mode = os.stat(endpoint_file()).st_mode & 0o777
        self.assertEqual(mode, 0o600, "token file must be owner-only")

    def test_endpoint_cleared_on_stop(self):
        self.server.stop()
        self.assertIsNone(read_endpoint())
        # stop() is idempotent for tearDown.
        self.server.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
