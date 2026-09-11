"""Tests for the hand-rolled WebSocket layer.

Run with:  python3 -m unittest discover -s daemon/tests -v
(stdlib unittest only — Codeville has no test dependencies either)
"""

import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from codeville.wsproto import (  # noqa: E402
    OP_CLOSE,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    Broadcaster,
    FrameDecoder,
    WebSocketConnection,
    WebSocketError,
    accept_key,
    encode_frame,
    handshake_response,
)


def mask_frame(payload: bytes, opcode: int = OP_TEXT, fin: bool = True) -> bytes:
    """Build a client->server (masked) frame, the way a browser would."""
    first = (0x80 if fin else 0x00) | opcode
    mask = os.urandom(4)
    masked = bytearray(payload)
    for i in range(len(masked)):
        masked[i] ^= mask[i & 3]
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", first, 0x80 | n)
    elif n < (1 << 16):
        header = struct.pack("!BBH", first, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", first, 0x80 | 127, n)
    return header + mask + bytes(masked)


class TestHandshake(unittest.TestCase):
    def test_accept_key_matches_rfc6455_example(self):
        # The canonical example from RFC 6455 section 1.3.
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_handshake_response_shape(self):
        raw = handshake_response("dGhlIHNhbXBsZSBub25jZQ==").decode()
        self.assertTrue(raw.startswith("HTTP/1.1 101 Switching Protocols\r\n"))
        self.assertIn("Upgrade: websocket\r\n", raw)
        self.assertIn("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n", raw)
        self.assertTrue(raw.endswith("\r\n\r\n"))

    def test_extra_headers_are_included(self):
        raw = handshake_response("dGhlIHNhbXBsZSBub25jZQ==", {"X-Codeville": "1"}).decode()
        self.assertIn("X-Codeville: 1\r\n", raw)


class TestEncodeFrame(unittest.TestCase):
    def test_short_payload_uses_7bit_length(self):
        frame = encode_frame(b"hi")
        self.assertEqual(frame, b"\x81\x02hi")

    def test_medium_payload_uses_16bit_length(self):
        frame = encode_frame(b"x" * 200)
        self.assertEqual(frame[:2], b"\x81\x7e")
        self.assertEqual(struct.unpack("!H", frame[2:4])[0], 200)

    def test_large_payload_uses_64bit_length(self):
        frame = encode_frame(b"x" * 70000)
        self.assertEqual(frame[:2], b"\x81\x7f")
        self.assertEqual(struct.unpack("!Q", frame[2:10])[0], 70000)

    def test_server_frames_are_never_masked(self):
        # bit 0x80 of byte 1 is the mask flag; servers must leave it clear.
        self.assertEqual(encode_frame(b"abc")[1] & 0x80, 0)


class TestFrameDecoder(unittest.TestCase):
    def test_single_text_frame(self):
        dec = FrameDecoder()
        out = list(dec.feed(mask_frame(b'{"a":1}')))
        self.assertEqual(out, [(OP_TEXT, b'{"a":1}')])

    def test_split_across_reads(self):
        """A frame arriving in three chunks must still decode exactly once."""
        frame = mask_frame(b"hello world")
        dec = FrameDecoder()
        self.assertEqual(list(dec.feed(frame[:3])), [])
        self.assertEqual(list(dec.feed(frame[3:7])), [])
        self.assertEqual(list(dec.feed(frame[7:])), [(OP_TEXT, b"hello world")])

    def test_two_frames_in_one_read(self):
        dec = FrameDecoder()
        out = list(dec.feed(mask_frame(b"one") + mask_frame(b"two")))
        self.assertEqual(out, [(OP_TEXT, b"one"), (OP_TEXT, b"two")])

    def test_fragmented_message_is_reassembled(self):
        dec = FrameDecoder()
        first = mask_frame(b"par", OP_TEXT, fin=False)
        cont = mask_frame(b"tial", 0x0, fin=True)
        out = list(dec.feed(first)) + list(dec.feed(cont))
        self.assertEqual(out, [(OP_TEXT, b"partial")])

    def test_control_frame_interleaved_in_fragment(self):
        dec = FrameDecoder()
        out = list(dec.feed(mask_frame(b"a", OP_TEXT, fin=False)))
        out += list(dec.feed(mask_frame(b"pp", OP_PING)))
        out += list(dec.feed(mask_frame(b"b", 0x0, fin=True)))
        self.assertEqual(out, [(OP_PING, b"pp"), (OP_TEXT, b"ab")])

    def test_64bit_length_roundtrip(self):
        payload = json.dumps({"big": "x" * 70000}).encode()
        dec = FrameDecoder()
        self.assertEqual(list(dec.feed(mask_frame(payload))), [(OP_TEXT, payload)])

    def test_unmasked_client_frame_is_rejected(self):
        dec = FrameDecoder()
        with self.assertRaises(WebSocketError):
            list(dec.feed(encode_frame(b"nope")))  # server-style, unmasked

    def test_oversized_message_is_rejected(self):
        dec = FrameDecoder(max_message_bytes=32)
        with self.assertRaises(WebSocketError):
            list(dec.feed(mask_frame(b"y" * 64)))

    def test_continuation_without_opener_is_rejected(self):
        dec = FrameDecoder()
        with self.assertRaises(WebSocketError):
            list(dec.feed(mask_frame(b"orphan", 0x0, fin=True)))


class TestConnectionRoundTrip(unittest.TestCase):
    """End-to-end over a real loopback socket pair."""

    def setUp(self):
        self.srv_sock, self.cli_sock = socket.socketpair()
        self.conn = WebSocketConnection(self.srv_sock, ("127.0.0.1", 0))

    def tearDown(self):
        for s in (self.srv_sock, self.cli_sock):
            try:
                s.close()
            except OSError:
                pass

    def test_text_roundtrip_and_ping_is_answered(self):
        received = []
        t = threading.Thread(
            target=self.conn.read_loop,
            args=(lambda c, m: received.append(m),),
            kwargs={"poll_timeout": 0.05},
            daemon=True,
        )
        t.start()

        self.cli_sock.sendall(mask_frame(json.dumps({"kind": "hello"}).encode()))
        self.cli_sock.sendall(mask_frame(b"beat", OP_PING))

        deadline = time.time() + 3
        pong = None
        dec = FrameDecoder()
        self.cli_sock.settimeout(0.2)
        while time.time() < deadline and pong is None:
            try:
                data = self.cli_sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            # server->client frames are unmasked, decode by hand
            if data[0] & 0x0F == OP_PONG:
                pong = data[2:2 + (data[1] & 0x7F)]

        self.assertEqual(received, ['{"kind": "hello"}'])
        self.assertEqual(pong, b"beat")

        self.conn.close()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())

    def test_close_frame_ends_the_loop(self):
        t = threading.Thread(target=self.conn.read_loop, args=(lambda c, m: None,),
                             kwargs={"poll_timeout": 0.05}, daemon=True)
        t.start()
        self.cli_sock.sendall(mask_frame(struct.pack("!H", 1000), OP_CLOSE))
        t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertTrue(self.conn.closed)

    def test_send_after_close_reports_failure(self):
        self.conn.close()
        self.assertFalse(self.conn.send_text("anyone there?"))


class TestBroadcaster(unittest.TestCase):
    def test_broadcast_counts_and_prunes_dead_connections(self):
        b = Broadcaster()
        live_pairs = []
        for _ in range(3):
            s1, s2 = socket.socketpair()
            live_pairs.append((s1, s2))
            b.add(WebSocketConnection(s1, ("127.0.0.1", 0)))
        self.assertEqual(len(b), 3)
        self.assertEqual(b.broadcast_text("ping"), 3)

        # Kill one peer; the next broadcast must notice and drop it.
        dead = next(iter(b.snapshot()))
        dead.close()
        self.assertEqual(b.broadcast_text("again"), 2)
        self.assertEqual(len(b), 2)

        for s1, s2 in live_pairs:
            for s in (s1, s2):
                try:
                    s.close()
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
