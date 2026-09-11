"""Discovering and following everything Claude Code writes.

The watcher is the bridge between files on disk and :class:`~codeville.world.World`.
It walks ``~/.claude/projects``, finds transcripts, subagent logs, workflow state and
journals, tails them, and pushes the resulting events out through a callback.

**Why polling rather than inotify.** inotify would need one watch per directory and
the session tree grows a new directory per session, per workflow, per agent. On this
machine ``fs.inotify.max_user_watches`` is a shared, exhaustible resource that editors
and file managers already consume, and silently hitting the ceiling would make
Codeville miss events with no error. Polling ``os.stat`` is O(files) with a tiny
constant: a full sweep of 28 projects measures in single-digit milliseconds, which is
far below the poll interval. Predictable beats clever here.

The cost is bounded by only tailing files that are *recently active* — a session that
has not been written to in :data:`ACTIVE_WINDOW` seconds is discovered but not
followed, so idle history never costs anything.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .projects import Project, discover_projects, default_projects_dir
from .records import agent_id_from_path, journal_entries
from .tailer import JsonlTailer
from .world import World

#: How often to sweep for new files and new records.
POLL_INTERVAL = 1.0

#: How often to re-scan for brand-new projects/sessions (cheaper than every poll).
DISCOVER_INTERVAL = 5.0

#: Only follow transcripts touched within this many seconds.
ACTIVE_WINDOW = 6 * 3600.0

#: How often to age out quiet villagers.
SWEEP_INTERVAL = 10.0


@dataclass
class SessionPaths:
    """Every file belonging to one session."""

    village_slug: str
    session_id: str
    transcript: str
    session_dir: str
    agent_logs: Set[str] = field(default_factory=set)
    agent_metas: Set[str] = field(default_factory=set)
    journals: Set[str] = field(default_factory=set)
    workflow_states: Set[str] = field(default_factory=set)


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _load_json(path: str) -> Optional[dict]:
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Watcher:
    """Polls the Claude Code data directory and feeds a :class:`World`."""

    def __init__(self, world: World, projects_dir: Optional[str] = None,
                 emit: Optional[Callable[[List[dict]], None]] = None,
                 poll_interval: float = POLL_INTERVAL,
                 active_window: float = ACTIVE_WINDOW) -> None:
        self.world = world
        self.projects_dir = projects_dir or default_projects_dir()
        self.emit = emit or (lambda events: None)
        self.poll_interval = poll_interval
        self.active_window = active_window

        self.tailer = JsonlTailer()
        self.sessions: Dict[str, SessionPaths] = {}
        #: Files whose whole content is re-read on change (small JSON, not JSONL).
        self._snapshot_mtimes: Dict[str, float] = {}
        self._meta_seen: Set[str] = set()

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_discover = 0.0
        self._last_sweep = 0.0
        #: Set on the first discovery pass so history is not replayed as "live".
        self._primed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "Watcher":
        self.prime()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="codeville-watcher")
        self._thread.start()
        return self

    def prime(self) -> None:
        """Build the opening state without emitting it as if it just happened.

        Discovery reads a tail of each active transcript, which resurrects agents
        that finished hours ago. Sweeping here retires them silently, so the first
        view shows only what is genuinely current instead of a burst of dozens of
        despawns a few seconds after connecting.
        """
        self.discover()
        self.drain()
        self.world.sweep()
        self._last_discover = time.time()
        self._last_sweep = time.time()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                # A watcher crash would silently freeze the village; keep going.
                pass
            self._stop.wait(self.poll_interval)

    def tick(self) -> List[dict]:
        """One poll cycle. Returns the events emitted (also handed to `emit`)."""
        moment = time.time()
        events: List[dict] = []

        if moment - self._last_discover >= DISCOVER_INTERVAL:
            events += self.discover()
            self._last_discover = moment

        events += self.drain()

        if moment - self._last_sweep >= SWEEP_INTERVAL:
            events += self.world.sweep(moment)
            self._last_sweep = moment

        if events:
            self.emit(events)
        return events

    # -- discovery ---------------------------------------------------------

    def discover(self) -> List[dict]:
        """Find new projects, sessions and per-session files."""
        events: List[dict] = []
        cutoff = time.time() - self.active_window

        for project in discover_projects(self.projects_dir):
            events += self.world.upsert_village(
                project.slug,
                name=project.name,
                path=project.path or "",
                exists=project.exists,
                stale=project.is_stale,
                biome=biome_for(project),
                last_active=project.last_active,
            )
            for transcript in project.session_files:
                if _mtime(transcript) < cutoff:
                    continue  # dormant: discovered, not followed
                events += self._track_session(project, transcript)

        # Newly appeared per-session files (subagents spawn mid-run).
        for paths in list(self.sessions.values()):
            self._rescan_session_dir(paths)

        self._primed = True
        return events

    def _track_session(self, project: Project, transcript: str) -> List[dict]:
        session_id = os.path.basename(transcript)[: -len(".jsonl")]
        key = f"{project.slug}/{session_id}"
        if key in self.sessions:
            return []

        session_dir = os.path.join(project.store_dir, session_id)
        paths = SessionPaths(village_slug=project.slug, session_id=session_id,
                             transcript=transcript, session_dir=session_dir)
        self.sessions[key] = paths

        # On first discovery open at EOF so old history is not replayed as if it
        # were happening now. Sessions found later started while we were running,
        # so those are read from the top.
        self.tailer.track(transcript, start_at_end=not self._primed)
        self._rescan_session_dir(paths)
        return []

    def _rescan_session_dir(self, paths: SessionPaths) -> None:
        """Pick up subagent logs, journals and workflow state files."""
        root = paths.session_dir
        if not os.path.isdir(root):
            return
        for dirpath, dirnames, filenames in os.walk(root):
            # tool-results holds large raw payloads we never render.
            dirnames[:] = [d for d in dirnames if d != "tool-results"]
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                if filename.endswith(".meta.json"):
                    paths.agent_metas.add(full)
                elif filename == "journal.jsonl":
                    if full not in paths.journals:
                        paths.journals.add(full)
                        self.tailer.track(full, start_at_end=not self._primed)
                elif filename.startswith("agent-") and filename.endswith(".jsonl"):
                    if full not in paths.agent_logs:
                        paths.agent_logs.add(full)
                        self.tailer.track(full, start_at_end=not self._primed)
                elif filename.startswith("wf_") and filename.endswith(".json"):
                    paths.workflow_states.add(full)

    # -- draining ----------------------------------------------------------

    def drain(self) -> List[dict]:
        """Read everything new and turn it into events."""
        events: List[dict] = []
        by_path = {p.transcript: p for p in self.sessions.values()}
        agent_owner: Dict[str, SessionPaths] = {}
        journal_owner: Dict[str, SessionPaths] = {}
        for paths in self.sessions.values():
            for log in paths.agent_logs:
                agent_owner[log] = paths
            for journal in paths.journals:
                journal_owner[journal] = paths

        for result in self.tailer.poll_all():
            path = result.path
            if path in by_path:
                paths = by_path[path]
                for record in result.records:
                    events += self.world.ingest_session_record(
                        paths.village_slug, paths.session_id, record)
            elif path in agent_owner:
                paths = agent_owner[path]
                agent_id = agent_id_from_path(path)
                for record in result.records:
                    events += self.world.ingest_agent_record(
                        paths.village_slug, paths.session_id, agent_id, record)
            elif path in journal_owner:
                paths = journal_owner[path]
                events += self.world.ingest_journal(
                    paths.village_slug, paths.session_id,
                    journal_entries(result.records))

        events += self._drain_snapshots()
        return events

    def _drain_snapshots(self) -> List[dict]:
        """Re-read whole-file JSON (agent meta, workflow state) when it changes."""
        events: List[dict] = []
        for paths in self.sessions.values():
            for meta_path in paths.agent_metas:
                stamp = _mtime(meta_path)
                if stamp <= self._snapshot_mtimes.get(meta_path, 0.0):
                    continue
                self._snapshot_mtimes[meta_path] = stamp
                meta = _load_json(meta_path)
                if meta is not None:
                    events += self.world.ingest_agent_meta(
                        paths.village_slug, paths.session_id,
                        agent_id_from_path(meta_path), meta)

            for wf_path in paths.workflow_states:
                stamp = _mtime(wf_path)
                if stamp <= self._snapshot_mtimes.get(wf_path, 0.0):
                    continue
                self._snapshot_mtimes[wf_path] = stamp
                state = _load_json(wf_path)
                if state is not None:
                    events += self.world.ingest_workflow(
                        paths.village_slug, paths.session_id, state)
        return events


#: Primary language -> village biome. Purely cosmetic, but stable per project.
LANGUAGE_BIOMES = {
    "python": "forest",
    "javascript": "harbor",
    "typescript": "harbor",
    "rust": "canyon",
    "go": "tundra",
    "java": "citadel",
    "kotlin": "citadel",
    "swift": "cliffs",
    "ruby": "orchard",
    "php": "bazaar",
    "c": "foundry",
    "cpp": "foundry",
    "shell": "workshop",
    "html": "meadow",
    "css": "meadow",
}

_EXTENSION_LANGUAGES = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".rs": "rust", ".go": "go",
    ".java": "java", ".kt": "kotlin", ".swift": "swift", ".rb": "ruby",
    ".php": "php", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".cc": "cpp", ".sh": "shell", ".bash": "shell", ".html": "html", ".css": "css",
}

_SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "target",
              "dist", "build", ".next", "vendor", "Pods", ".cache"}


def detect_language(path: str, max_entries: int = 400) -> str:
    """Cheap top-level language census.

    Deliberately shallow (two directory levels, capped file count) because this
    runs for every village and must never stall the daemon on a huge repo.
    """
    if not path or not os.path.isdir(path):
        return ""
    counts: Dict[str, int] = {}
    seen = 0
    for depth_root, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".")]
        if depth_root[len(path):].count(os.sep) >= 2:
            dirnames[:] = []
        for filename in filenames:
            language = _EXTENSION_LANGUAGES.get(os.path.splitext(filename)[1].lower())
            if language:
                counts[language] = counts.get(language, 0) + 1
            seen += 1
            if seen >= max_entries:
                dirnames[:] = []
                break
        if seen >= max_entries:
            break
    return max(counts, key=counts.get) if counts else ""


def biome_for(project: Project) -> str:
    """Pick a stable biome for a village.

    Language first so related repos look related; otherwise a hash of the slug, so
    the same project always gets the same look across restarts and machines.
    """
    language = detect_language(project.path or "")
    if language in LANGUAGE_BIOMES:
        return LANGUAGE_BIOMES[language]
    biomes = sorted(set(LANGUAGE_BIOMES.values()))
    digest = sum(ord(c) * (i + 1) for i, c in enumerate(project.slug))
    return biomes[digest % len(biomes)]
