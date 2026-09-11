"""Wiring: world + watcher + server, and the single-instance lock."""

from __future__ import annotations

import errno
import json
import os
import signal
import sys
import threading
import time
from typing import Optional

from .server import CodevilleServer, read_endpoint, runtime_dir
from .watcher import Watcher
from .world import World

#: Bundled web UI, resolved relative to this package (repo layout: daemon/codeville).
DEFAULT_WEB_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "web"))


def lock_path() -> str:
    return os.path.join(runtime_dir(), "daemon.pid")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM  # exists but owned by someone else
    return True


def existing_instance() -> Optional[dict]:
    """Return the running daemon's endpoint, or None.

    A stale pid file (daemon killed without cleanup) is treated as absent so the
    tray can always recover without the user hunting for a lock file.
    """
    try:
        with open(lock_path()) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    if not _process_alive(pid) or pid == os.getpid():
        return None
    return read_endpoint()


def write_lock() -> None:
    path = lock_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))


def clear_lock() -> None:
    try:
        os.remove(lock_path())
    except OSError:
        pass


class Codeville:
    """The daemon: watches Claude Code and serves the village."""

    def __init__(self, web_root: str = DEFAULT_WEB_ROOT, port: int = 0,
                 projects_dir: Optional[str] = None,
                 poll_interval: float = 1.0) -> None:
        self.world = World()
        self.server = CodevilleServer(
            web_root=web_root,
            port=port,
            state_provider=self.world.snapshot,
            on_client_message=self._on_client_message,
        )
        self.watcher = Watcher(
            self.world,
            projects_dir=projects_dir,
            emit=self._emit,
            poll_interval=poll_interval,
        )
        self._stop = threading.Event()

    # -- plumbing ----------------------------------------------------------

    def _emit(self, events) -> None:
        if not self.server.client_count:
            return  # nobody watching; state is still updated in the World
        for event in events:
            self.server.broadcast(event)

    def _on_client_message(self, payload: dict) -> None:
        # The UI is read-only today; "ping" keeps proxies from idling us out.
        if payload.get("t") == "ping":
            self.server.broadcast({"t": "pong", "at": time.time()})

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "Codeville":
        self.server.start()
        write_lock()
        self.watcher.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.watcher.stop()
        self.server.stop()
        clear_lock()

    def run_forever(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self._stop.set())
            except ValueError:
                pass  # not on the main thread (embedded in the tray)
        try:
            while not self._stop.is_set():
                self._stop.wait(0.5)
        finally:
            self.stop()


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="codeville",
        description="Watch Claude Code work, as a village.")
    parser.add_argument("--port", type=int, default=0,
                        help="TCP port (default: an unused one chosen by the OS)")
    parser.add_argument("--web-root", default=DEFAULT_WEB_ROOT,
                        help="directory holding the web UI")
    parser.add_argument("--projects-dir", default=None,
                        help="override ~/.claude/projects")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="seconds between filesystem polls")
    parser.add_argument("--print-url", action="store_true",
                        help="print the authenticated URL and keep running")
    parser.add_argument("--once", action="store_true",
                        help="run a single scan, print a summary, and exit")
    args = parser.parse_args(argv)

    if args.once:
        world = World()
        watcher = Watcher(world, projects_dir=args.projects_dir)
        watcher.discover()
        watcher.drain()
        print(json.dumps(world.stats(), indent=2))
        return 0

    running = existing_instance()
    if running:
        print(f"Codeville is already running at {running.get('url')}", file=sys.stderr)
        return 3

    app = Codeville(web_root=args.web_root, port=args.port,
                    projects_dir=args.projects_dir,
                    poll_interval=args.poll_interval).start()
    print(f"Codeville is watching. Open: {app.server.url}", flush=True)
    if args.print_url:
        print(app.server.url, flush=True)
    app.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
