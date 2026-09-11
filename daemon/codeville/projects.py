"""Discovering Claude Code projects and mapping them back to real directories.

Claude Code stores one directory per working directory under
``~/.claude/projects/``, named by flattening the path::

    /home/ali/Documents/rama.framer.media-178  ->  -home-ali-Documents-rama-framer-media-178
    /home/ali/Pictures/Beeb-all                ->  -home-ali-Pictures-Beeb-all
    /home/ali/Documents/Netorase_Phone_-_Iris  ->  -home-ali-Documents-Netorase-Phone---Iris

**The encoding is lossy.** ``/``, ``.`` and ``_`` all collapse to ``-``, and a literal
``-`` survives as ``-``, so a slug cannot be reversed by string surgery alone: given
``-home-ali-Pictures-Beeb-all`` there is no way to tell ``Beeb-all`` from ``Beeb/all``.

So Codeville resolves a project in two steps:

1. **Read ``cwd`` out of the transcript.** Every ``user``/``assistant`` record carries
   the absolute working directory. This is ground truth and costs ~10 ms for every
   project on disk, because we stop at the first match.
2. **Probe the filesystem** only when a project has no transcripts at all (stale
   directories left behind by deleted projects). We walk the slug segment by segment,
   trying each separator that could have produced a ``-``, and keep the branches that
   exist on disk.

Measured on the author's machine: 28 project directories, 12 with transcripts — all 12
resolved exactly from ``cwd``; the other 16 had no ``.jsonl`` files at all.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

#: Separators that Claude Code flattens into "-" when building a slug.
_AMBIGUOUS_SEPARATORS = ("/", "-", ".", "_")

#: Cap the fallback search so a pathological slug cannot hang the daemon.
_MAX_PROBE_BRANCHES = 4000


def default_projects_dir() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


@dataclass
class Project:
    """One Claude Code project directory — one village in the UI."""

    slug: str
    #: Absolute path to the project dir under ~/.claude/projects.
    store_dir: str
    #: The real working directory, if we could determine it.
    path: Optional[str] = None
    #: How `path` was determined: "cwd" (authoritative), "probe" (guessed), or "" .
    resolved_by: str = ""
    #: Transcript files directly in the project dir.
    session_files: List[str] = field(default_factory=list)
    #: True when the resolved directory still exists on disk.
    exists: bool = False

    @property
    def name(self) -> str:
        """Short display name — the village's name."""
        if self.path:
            return os.path.basename(self.path.rstrip("/")) or self.path
        return self.slug.lstrip("-").split("-")[-1] or self.slug

    @property
    def is_stale(self) -> bool:
        """No transcripts left: an abandoned village."""
        return not self.session_files

    @property
    def last_active(self) -> float:
        """mtime of the most recently written transcript, or 0."""
        best = 0.0
        for f in self.session_files:
            try:
                best = max(best, os.path.getmtime(f))
            except OSError:
                continue
        return best

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "path": self.path,
            "resolved_by": self.resolved_by,
            "exists": self.exists,
            "stale": self.is_stale,
            "session_count": len(self.session_files),
            "last_active": self.last_active,
        }


def read_cwd_from_transcript(jsonl_path: str, max_lines: int = 4000) -> Optional[str]:
    """Pull the first ``cwd`` out of a transcript.

    Scans bytewise and only parses lines that contain the key, so a 137 MB transcript
    costs a few milliseconds. Gives up after *max_lines* — ``cwd`` appears on the very
    first conversational record, so if it is not near the top it is not there at all.
    """
    try:
        with open(jsonl_path, "rb") as fh:
            for index, line in enumerate(fh):
                if index > max_lines:
                    return None
                if b'"cwd"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                cwd = record.get("cwd")
                if isinstance(cwd, str) and cwd.startswith("/"):
                    return cwd
    except OSError:
        return None
    return None


def probe_path_from_slug(slug: str, max_branches: int = _MAX_PROBE_BRANCHES) -> Optional[str]:
    """Best-effort reconstruction of a path from a slug, by testing the filesystem.

    Only used for projects with no transcripts. Walks left to right, and at every
    ``-`` tries each separator that could have produced it, keeping only branches that
    exist on disk. Returns the longest fully-existing path, or None.
    """
    tokens = slug.lstrip("-").split("-")
    if not tokens:
        return None

    # Each candidate is (path_so_far, index_of_next_token).
    candidates: List[Tuple[str, int]] = [("/" + tokens[0], 1)]
    complete: List[str] = []
    branches = 0

    while candidates and branches < max_branches:
        path, index = candidates.pop()
        branches += 1

        if index >= len(tokens):
            if os.path.isdir(path):
                complete.append(path)
            continue

        token = tokens[index]
        for separator in _AMBIGUOUS_SEPARATORS:
            nxt = path + separator + token
            # Prune aggressively: keep a branch only if it is a real path on disk, or
            # the prefix of one. The prefix case matters because a single directory
            # name can span several tokens — "/tmp" + "/" + "codeville" is not yet a
            # directory when the real name is "/tmp/codeville-proj-xxxx".
            if separator == "/" and not os.path.isdir(path):
                continue
            if os.path.exists(nxt) or _has_children_with_prefix(nxt):
                candidates.append((nxt, index + 1))

    if not complete:
        return None
    return max(complete, key=len)


def _has_children_with_prefix(partial: str) -> bool:
    """True if some entry in the parent dir starts with *partial*'s basename.

    Lets a multi-token name survive the walk before it is complete, e.g. keeping
    ``/home/a/Beeb`` alive while the real directory is ``/home/a/Beeb-all``.
    """
    parent, base = os.path.split(partial)
    if not base:
        return False
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name.startswith(base):
                    return True
    except OSError:
        return False
    return False


def resolve_project(store_dir: str) -> Project:
    """Build a :class:`Project` for one directory under ``~/.claude/projects``."""
    slug = os.path.basename(store_dir.rstrip("/"))
    sessions = sorted(
        glob.glob(os.path.join(store_dir, "*.jsonl")),
        key=lambda p: _safe_mtime(p),
        reverse=True,
    )
    project = Project(slug=slug, store_dir=store_dir, session_files=sessions)

    for transcript in sessions:  # newest first — likeliest to be valid
        cwd = read_cwd_from_transcript(transcript)
        if cwd:
            project.path, project.resolved_by = cwd, "cwd"
            break

    if project.path is None:
        guessed = probe_path_from_slug(slug)
        if guessed:
            project.path, project.resolved_by = guessed, "probe"

    project.exists = bool(project.path) and os.path.isdir(project.path)
    return project


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def discover_projects(projects_dir: Optional[str] = None,
                      include_stale: bool = True) -> List[Project]:
    """Resolve every project directory, newest activity first."""
    root = projects_dir or default_projects_dir()
    found: List[Project] = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return found

    for entry in entries:
        store_dir = os.path.join(root, entry)
        if not os.path.isdir(store_dir):
            continue
        project = resolve_project(store_dir)
        if project.is_stale and not include_stale:
            continue
        found.append(project)

    found.sort(key=lambda p: p.last_active, reverse=True)
    return found


def index_by_path(projects: Iterable[Project]) -> Dict[str, Project]:
    """Map real working directory -> project, for looking a village up by cwd."""
    return {p.path: p for p in projects if p.path}
