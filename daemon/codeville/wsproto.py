"""Minimal RFC 6455 WebSocket server built on the Python standard library.

Codeville ships with zero third-party dependencies so that it can be installed on
any distro with nothing but the system Python and GTK. That rules out `websockets`
and friends, so the small slice of RFC 6455 we actually need lives here.

Scope on purpose: text frames, ping/pong, close, fragmentation and the 16/64-bit
length forms. No permessage-deflate, no extensions, no client role.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
from typing import Callable, Iterable, Iterator, Optional, Tuple

#: Magic value from RFC 6455 §1.3, concatenated with the client key before hashing.
#: Transcribe it carefully — the final group is C5AB0DC85B11. Getting it wrong still
#: round-trips against your own client, but every real browser rejects the handshake.
#: `tests/test_wsproto.py` pins it to the RFC's worked example.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: Refuse to buffer a single message larger than this (bytes). A Codeville event is
#: a few KB; anything near this ceiling means a bug or a hostile client.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class WebSocketError(Exception):
    """Protocol violation or oversized frame; the caller should drop the socket."""


def accept_key(client_key: str) -> str:
    """Compute the `Sec-WebSocket-Accept` response value for *client_key*."""
    digest = hashlib.sha1((client_key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def handshake_response(client_key: str, extra_headers: Optional[dict] = None) -> bytes:
    """Build the full HTTP 101 response completing the upgrade."""
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept_key(client_key)}",
    ]
    for key, value in (extra_headers or {}).items():
        lines.append(f"{key}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def encode_frame(payload: bytes, opcode: int = OP_TEXT) -> bytes:
    """Encode one *unfragmented* server->client frame.

    Server frames are never masked (RFC 6455 §5.1).
    """
    first = 0x80 | opcode  # FIN set
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", first, length)
    elif length < (1 << 16):
        header = struct.pack("!BBH", first, 126, length)
    else:
        header = struct.pack("!BBQ", first, 127, length)
    return header + payload


def encode_close(code: int = 1000, reason: str = "") -> bytes:
    return encode_frame(struct.pack("!H", code) + reason.encode("utf-8"), OP_CLOSE)


class FrameDecoder:
    """Incremental client->server frame decoder.

    Feed it whatever ``recv()`` returned; it yields ``(opcode, payload)`` for each
    *complete* message. Continuation frames are reassembled, so a yielded opcode is
    always one of TEXT/BINARY/CLOSE/PING/PONG.
    """

    def __init__(self, max_message_bytes: int = MAX_MESSAGE_BYTES) -> None:
        self._buf = bytearray()
        self._max = max_message_bytes
        self._frag_opcode: Optional[int] = None
        self._frag = bytearray()

    def feed(self, data: bytes) -> Iterator[Tuple[int, bytes]]:
        self._buf += data
        while True:
            frame = self._take_frame()
            if frame is None:
                return
            fin, opcode, payload = frame

            if opcode in (OP_CLOSE, OP_PING, OP_PONG):
                # Control frames may interleave with a fragmented message.
                yield opcode, payload
                continue

            if opcode == OP_CONT:
                if self._frag_opcode is None:
                    raise WebSocketError("continuation frame without an opener")
                self._frag += payload
                self._guard_size(len(self._frag))
                if fin:
                    opcode, self._frag_opcode = self._frag_opcode, None
                    payload, self._frag = bytes(self._frag), bytearray()
                    yield opcode, payload
                continue

            # A fresh data frame.
            if self._frag_opcode is not None:
                raise WebSocketError("new data frame while a message was fragmented")
            if fin:
                yield opcode, payload
            else:
                self._frag_opcode = opcode
                self._frag = bytearray(payload)
                self._guard_size(len(self._frag))

    def _guard_size(self, size: int) -> None:
        if size > self._max:
            raise WebSocketError(f"message exceeds {self._max} bytes")

    def _take_frame(self) -> Optional[Tuple[bool, int, bytes]]:
        buf = self._buf
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        offset = 2

        if length == 126:
            if len(buf) < offset + 2:
                return None
            (length,) = struct.unpack_from("!H", buf, offset)
            offset += 2
        elif length == 127:
            if len(buf) < offset + 8:
                return None
            (length,) = struct.unpack_from("!Q", buf, offset)
            offset += 8

        self._guard_size(length)

        mask = b""
        if masked:
            if len(buf) < offset + 4:
                return None
            mask = bytes(buf[offset:offset + 4])
            offset += 4
        else:
            # RFC 6455 §5.1: every client frame MUST be masked.
            raise WebSocketError("unmasked frame from client")

        if len(buf) < offset + length:
            return None

        payload = bytearray(buf[offset:offset + length])
        for i in range(length):
            payload[i] ^= mask[i & 3]
        del buf[:offset + length]
        return fin, opcode, bytes(payload)


class WebSocketConnection:
    """A single upgraded connection. Sends are serialized by a lock."""

    def __init__(self, sock: socket.socket, addr) -> None:
        self.sock = sock
        self.addr = addr
        self.id = base64.urlsafe_b64encode(os.urandom(9)).decode("ascii")
        self._send_lock = threading.Lock()
        self._closed = threading.Event()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def send_text(self, text: str) -> bool:
        return self._send(encode_frame(text.encode("utf-8"), OP_TEXT))

    def ping(self, payload: bytes = b"") -> bool:
        return self._send(encode_frame(payload, OP_PING))

    def _send(self, raw: bytes) -> bool:
        if self._closed.is_set():
            return False
        with self._send_lock:
            try:
                self.sock.sendall(raw)
                return True
            except OSError:
                self.close()
                return False

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            with self._send_lock:
                self.sock.sendall(encode_close(code, reason))
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def read_loop(self, on_text: Callable[["WebSocketConnection", str], None],
                  poll_timeout: float = 0.5) -> None:
        """Block reading frames until the peer goes away.

        Control frames are answered here; text messages go to *on_text*.
        """
        decoder = FrameDecoder()
        self.sock.settimeout(poll_timeout)
        try:
            while not self._closed.is_set():
                try:
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                for opcode, payload in decoder.feed(chunk):
                    if opcode == OP_CLOSE:
                        self.close()
                        return
                    if opcode == OP_PING:
                        self._send(encode_frame(payload, OP_PONG))
                    elif opcode == OP_TEXT:
                        on_text(self, payload.decode("utf-8", "replace"))
                    # BINARY and PONG are ignored: the UI speaks JSON text only.
        except WebSocketError:
            self.close(1009, "protocol error")
        finally:
            self.close()


class Broadcaster:
    """Fan-out registry of live connections, safe to call from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: set = set()

    def add(self, conn: WebSocketConnection) -> None:
        with self._lock:
            self._conns.add(conn)

    def discard(self, conn: WebSocketConnection) -> None:
        with self._lock:
            self._conns.discard(conn)

    def __len__(self) -> int:
        with self._lock:
            return len(self._conns)

    def snapshot(self) -> Iterable[WebSocketConnection]:
        with self._lock:
            return tuple(self._conns)

    def broadcast_text(self, text: str) -> int:
        """Send *text* to every live connection; returns how many got it."""
        delivered = 0
        for conn in self.snapshot():
            if conn.send_text(text):
                delivered += 1
            else:
                self.discard(conn)
        return delivered
