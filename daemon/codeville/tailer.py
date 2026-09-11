"""Incremental, restart-safe tailing of Claude Code's JSONL transcripts.

Claude Code appends newline-delimited JSON to
``~/.claude/projects/<slug>/<session>.jsonl`` and to the per-agent transcripts
under ``<session>/subagents/``. Codeville follows those files to learn what every
agent is doing right now.

Three facts drive the design:

* **The files get big.** The largest transcript on the author's machine is 137 MB.
  Replaying that on startup would stall the UI for seconds and tell the user nothing
  about *now*, so a tail normally opens at EOF (``start_at_end``) and only history
  scans read from byte zero.
* **A single line can be megabytes.** One record holds a whole tool result, so a
  naive ``readlines()`` can blow up memory. Lines past ``max_line_bytes`` are
  reported as oversized and skipped rather than buffered forever.
* **Writes are not atomic.** A read can land mid-line, so a trailing fragment is
  held back until its newline arrives.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

#: Skip any single line longer than this. Real records are well under it; anything
#: larger is a giant embedded tool result we would not render anyway.
MAX_LINE_BYTES = 4 * 1024 * 1024

#: How much of the tail to rewind when opening a file "at the end", so a session
#: that is mid-write still yields its last few records for immediate context.
TAIL_PRIMING_BYTES = 256 * 1024


@dataclass
class FileCursor:
    """Where we are inside one JSONL file."""

    path: str
    offset: int = 0
    inode: Optional[int] = None
    #: Bytes of a trailing line that has not been terminated by "\n" yet.
    partial: bytes = b""
    #: Lines skipped for exceeding MAX_LINE_BYTES, kept for diagnostics.
    skipped_oversized: int = 0

    def to_state(self) -> dict:
        return {"path": self.path, "offset": self.offset, "inode": self.inode}

    @classmethod
    def from_state(cls, data: dict) -> "FileCursor":
        return cls(path=data["path"], offset=int(data.get("offset", 0)),
                   inode=data.get("inode"))


@dataclass
class TailResult:
    """What one poll of one file produced."""

    path: str
    records: List[dict] = field(default_factory=list)
    #: Set when the file was replaced or truncated and the cursor was reset.
    reset: bool = False
    malformed: int = 0
    oversized: int = 0


class JsonlTailer:
    """Follows a set of JSONL files, yielding newly appended records.

    Not thread-safe; drive it from a single watcher thread.
    """

    def __init__(self, max_line_bytes: int = MAX_LINE_BYTES,
                 read_chunk: int = 1 << 20) -> None:
        self._cursors: Dict[str, FileCursor] = {}
        self._max_line = max_line_bytes
        self._chunk = read_chunk

    # ---- cursor management -------------------------------------------------

    def track(self, path: str, start_at_end: bool = True,
              prime_bytes: int = TAIL_PRIMING_BYTES) -> bool:
        """Begin following *path*. Returns True if it was newly added.

        With ``start_at_end`` the cursor opens near EOF (rewound by *prime_bytes*
        to a line boundary) so we surface current activity instead of replaying
        the whole history.
        """
        if path in self._cursors:
            return False
        try:
            st = os.stat(path)
        except OSError:
            return False

        offset = 0
        if start_at_end and st.st_size > prime_bytes:
            offset = self._align_to_line_start(path, st.st_size - prime_bytes)
        self._cursors[path] = FileCursor(path=path, offset=offset, inode=st.st_ino)
        return True

    def forget(self, path: str) -> None:
        self._cursors.pop(path, None)

    @property
    def tracked(self) -> Tuple[str, ...]:
        return tuple(self._cursors)

    def _align_to_line_start(self, path: str, approx: int) -> int:
        """Move *approx* forward to just past the next newline."""
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, approx))
                chunk = fh.read(self._chunk)
        except OSError:
            return 0
        nl = chunk.find(b"\n")
        return max(0, approx) + (nl + 1 if nl >= 0 else len(chunk))

    # ---- reading -----------------------------------------------------------

    def poll(self, path: str) -> TailResult:
        """Read everything appended to *path* since the last poll."""
        result = TailResult(path=path)
        cursor = self._cursors.get(path)
        if cursor is None:
            return result

        try:
            st = os.stat(path)
        except OSError:
            return result

        # Replaced (new inode) or truncated (shrunk): restart from the top.
        if cursor.inode is not None and st.st_ino != cursor.inode:
            cursor.offset, cursor.inode, cursor.partial = 0, st.st_ino, b""
            result.reset = True
        elif st.st_size < cursor.offset:
            cursor.offset, cursor.partial = 0, b""
            result.reset = True

        if st.st_size == cursor.offset:
            return result

        try:
            with open(path, "rb") as fh:
                fh.seek(cursor.offset)
                data = fh.read(st.st_size - cursor.offset)
        except OSError:
            return result

        cursor.offset += len(data)
        buf = cursor.partial + data
        cursor.partial = b""

        *lines, tail = buf.split(b"\n")
        # `tail` is whatever followed the last newline: an incomplete record.
        if len(tail) > self._max_line:
            cursor.skipped_oversized += 1
            result.oversized += 1  # abandon a runaway unterminated line
        else:
            cursor.partial = tail

        for raw in lines:
            if not raw.strip():
                continue
            if len(raw) > self._max_line:
                cursor.skipped_oversized += 1
                result.oversized += 1
                continue
            try:
                result.records.append(json.loads(raw))
            except (ValueError, UnicodeDecodeError):
                result.malformed += 1
        return result

    def poll_all(self) -> Iterator[TailResult]:
        for path in tuple(self._cursors):
            res = self.poll(path)
            if res.records or res.reset or res.malformed or res.oversized:
                yield res

    # ---- persistence -------------------------------------------------------

    def save_state(self) -> dict:
        return {"cursors": [c.to_state() for c in self._cursors.values()]}

    def load_state(self, data: dict) -> None:
        """Restore cursors from :meth:`save_state`, dropping stale entries.

        A file that shrank or was replaced since the state was written is reset to
        zero so nothing is silently skipped.
        """
        for entry in data.get("cursors", []):
            cursor = FileCursor.from_state(entry)
            try:
                st = os.stat(cursor.path)
            except OSError:
                continue
            if cursor.inode is not None and st.st_ino != cursor.inode:
                cursor.offset, cursor.inode = 0, st.st_ino
            elif st.st_size < cursor.offset:
                cursor.offset = 0
            self._cursors[cursor.path] = cursor


def read_last_records(path: str, limit: int = 50,
                      window: int = TAIL_PRIMING_BYTES,
                      max_line_bytes: int = MAX_LINE_BYTES) -> List[dict]:
    """Return up to *limit* trailing records without reading the whole file.

    Used to hydrate a session's recent history on connect. Widens its read window
    up to 8x if the tail did not contain enough complete records.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return []

    records: List[dict] = []
    for factor in (1, 4, 8):
        span = min(size, window * factor)
        try:
            with open(path, "rb") as fh:
                fh.seek(size - span)
                chunk = fh.read(span)
        except OSError:
            return []
        if span < size:
            # Drop the first (probably partial) line.
            nl = chunk.find(b"\n")
            chunk = chunk[nl + 1:] if nl >= 0 else b""

        records = []
        for raw in chunk.split(b"\n"):
            if not raw.strip() or len(raw) > max_line_bytes:
                continue
            try:
                records.append(json.loads(raw))
            except (ValueError, UnicodeDecodeError):
                continue
        if len(records) >= limit or span >= size:
            break
    return records[-limit:]
