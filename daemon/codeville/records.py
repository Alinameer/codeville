"""Reading meaning out of raw Claude Code transcript records.

Pure functions only — no I/O, no state. Everything here takes a decoded JSONL
record (or a message content block) and answers a question the village needs:
*who is this, what are they doing, did it work.*

The shapes are taken from real transcripts rather than documentation, so the
accessors are defensive: unknown record types, missing keys and surprising value
types must never raise, because a parser crash would freeze the whole view.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --------------------------------------------------------------------------
# Record classification
# --------------------------------------------------------------------------

#: Record types that carry conversational content we care about.
CONVERSATIONAL = frozenset({"user", "assistant"})

#: Types that exist in transcripts but say nothing about visible activity.
IGNORED_TYPES = frozenset({
    "queue-operation", "file-history-snapshot", "file-history-delta",
    "last-prompt", "atis-latch", "ai-title", "mode", "attachment",
})


def record_type(record: dict) -> str:
    value = record.get("type")
    return value if isinstance(value, str) else ""


def is_sidechain(record: dict) -> bool:
    """True for records produced inside a subagent rather than the main thread."""
    return bool(record.get("isSidechain"))


def parse_timestamp(value: Any) -> Optional[float]:
    """ISO-8601 (with Z or offset) -> POSIX seconds. Returns None if unparseable."""
    if isinstance(value, (int, float)):
        # Heuristic: values that large are milliseconds.
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def record_time(record: dict) -> Optional[float]:
    return parse_timestamp(record.get("timestamp"))


def content_blocks(record: dict) -> List[dict]:
    """The message content blocks, normalized to a list of dicts."""
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def message_text(record: dict, limit: int = 400) -> str:
    """Concatenated visible text of a message, collapsed to one line."""
    parts = [
        block.get("text", "")
        for block in content_blocks(record)
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    joined = " ".join(" ".join(parts).split())
    return joined[:limit]


def usage(record: dict) -> Dict[str, int]:
    """Token usage off an assistant record; empty dict when absent."""
    message = record.get("message")
    if not isinstance(message, dict):
        return {}
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return {}
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens")
    return {k: int(raw[k]) for k in keys if isinstance(raw.get(k), (int, float))}


def model_name(record: dict) -> str:
    message = record.get("message")
    if isinstance(message, dict) and isinstance(message.get("model"), str):
        return message["model"]
    return ""


# --------------------------------------------------------------------------
# Tool calls
# --------------------------------------------------------------------------

def tool_uses(record: dict) -> List[dict]:
    """Every ``tool_use`` block in an assistant record."""
    return [b for b in content_blocks(record) if b.get("type") == "tool_use"]


def tool_results(record: dict) -> List[dict]:
    """Every ``tool_result`` block in a user record."""
    return [b for b in content_blocks(record) if b.get("type") == "tool_result"]


def tool_result_is_error(block: dict) -> bool:
    """Whether a tool_result reports failure.

    Claude Code marks failures with ``is_error``, but some tools report trouble
    only in their text, so an explicit flag wins and the text is a fallback.
    """
    if block.get("is_error") is True:
        return True
    content = block.get("content")
    if isinstance(content, str):
        head = content[:200].lstrip().lower()
        return head.startswith("error:") or head.startswith("error ")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                head = part["text"][:200].lstrip().lower()
                if head.startswith("error:") or head.startswith("error "):
                    return True
    return False


def _short_path(value: Any, keep: int = 2) -> str:
    """Trim an absolute path down to its last *keep* segments for a label."""
    if not isinstance(value, str) or not value:
        return ""
    parts = [p for p in value.rstrip("/").split("/") if p]
    return "/".join(parts[-keep:]) if parts else value


def _first_line(value: Any, limit: int = 72) -> str:
    if not isinstance(value, str):
        return ""
    line = value.strip().splitlines()[0] if value.strip() else ""
    return line[:limit]


#: How to build a human label per tool, in priority order of input fields.
_TOOL_LABELS = {
    "Bash": lambda i: i.get("description") or _first_line(i.get("command")),
    "Read": lambda i: _short_path(i.get("file_path")),
    "Edit": lambda i: _short_path(i.get("file_path")),
    "Write": lambda i: _short_path(i.get("file_path")),
    "NotebookEdit": lambda i: _short_path(i.get("notebook_path")),
    "Glob": lambda i: str(i.get("pattern") or ""),
    "Grep": lambda i: str(i.get("pattern") or ""),
    "WebSearch": lambda i: str(i.get("query") or ""),
    "WebFetch": lambda i: str(i.get("url") or ""),
    "Task": lambda i: str(i.get("description") or i.get("subagent_type") or ""),
    "Agent": lambda i: str(i.get("description") or i.get("subagent_type") or ""),
    "Skill": lambda i: str(i.get("skill") or ""),
    "Workflow": lambda i: str(i.get("name") or i.get("description") or ""),
    "TodoWrite": lambda i: _todo_label(i),
    "ToolSearch": lambda i: str(i.get("query") or ""),
    "AskUserQuestion": lambda i: "asking you something",
}


def _todo_label(inputs: dict) -> str:
    todos = inputs.get("todos")
    if isinstance(todos, list) and todos:
        active = [t for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"]
        chosen = active[0] if active else todos[0]
        if isinstance(chosen, dict):
            for key in ("activeForm", "content"):
                if isinstance(chosen.get(key), str):
                    return chosen[key][:72]
        return f"{len(todos)} todos"
    return "planning"


def tool_label(name: str, inputs: Any, limit: int = 72) -> str:
    """A short, human phrase describing one tool call — the speech bubble text."""
    if not isinstance(inputs, dict):
        return ""
    builder = _TOOL_LABELS.get(name)
    label = ""
    if builder is not None:
        try:
            label = builder(inputs) or ""
        except Exception:
            label = ""
    if not label:
        # Generic fallback: the first short string value in the input.
        for key in ("description", "prompt", "query", "path", "file_path", "name"):
            candidate = inputs.get(key)
            if isinstance(candidate, str) and candidate.strip():
                label = _first_line(candidate, limit)
                break
    return " ".join(str(label).split())[:limit]


#: Tools grouped by the kind of work they represent. The UI picks the villager's
#: prop and working animation from the group, so an unrecognised tool still gets
#: sensible behaviour via "other".
TOOL_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "run": ("Bash", "BashOutput", "KillShell"),
    "read": ("Read", "NotebookRead"),
    "write": ("Write", "NotebookEdit"),
    "edit": ("Edit", "MultiEdit"),
    "search": ("Grep", "Glob", "ToolSearch"),
    "web": ("WebSearch", "WebFetch"),
    "plan": ("TodoWrite", "ExitPlanMode", "EnterPlanMode"),
    "summon": ("Task", "Agent", "Workflow", "Skill"),
    "ask": ("AskUserQuestion",),
}

_CATEGORY_BY_TOOL = {
    tool: category for category, tools in TOOL_CATEGORIES.items() for tool in tools
}


def tool_category(name: str) -> str:
    """Which prop/animation family a tool belongs to."""
    if not isinstance(name, str):
        return "other"
    if name in _CATEGORY_BY_TOOL:
        return _CATEGORY_BY_TOOL[name]
    if name.startswith("mcp__"):
        return "mcp"
    return "other"


def tool_display_name(name: str) -> str:
    """A readable name for a tool.

    MCP tools arrive as ``mcp__claude_ai_pletor_mcp__generate_image``, which is far
    too long for a signpost, so they become ``pletor: generate image``.
    """
    if not isinstance(name, str):
        return ""
    if not name.startswith("mcp__"):
        return name
    parts = [p for p in name.split("__") if p]
    if len(parts) < 2:
        return name
    server = parts[1]
    for prefix in ("claude_ai_", "claude-ai-"):
        if server.startswith(prefix):
            server = server[len(prefix):]
    for suffix in ("_mcp", "-mcp"):
        if server.endswith(suffix):
            server = server[: -len(suffix)]
    server = server.replace("_", " ").replace("-", " ").strip()
    tool = parts[2].replace("_", " ").replace("-", " ").strip() if len(parts) > 2 else ""
    if server and tool:
        return f"{server}: {tool}"
    return tool or server or name


def describe_tool_use(block: dict) -> Optional[dict]:
    """Normalize a ``tool_use`` block into the facts the village needs."""
    name = block.get("name")
    if not isinstance(name, str):
        return None
    inputs = block.get("input")
    return {
        "id": block.get("id") if isinstance(block.get("id"), str) else "",
        "tool": name,
        "display": tool_display_name(name),
        "category": tool_category(name),
        "label": tool_label(name, inputs),
        "spawns_agent": name in ("Task", "Agent", "Workflow"),
        "subagent_type": (inputs or {}).get("subagent_type") if isinstance(inputs, dict) else None,
    }


# --------------------------------------------------------------------------
# Subagent metadata (agent-<id>.meta.json)
# --------------------------------------------------------------------------

def agent_id_from_path(path: str) -> str:
    """``.../agent-a1b2c3.jsonl`` -> ``a1b2c3``."""
    base = os.path.basename(path)
    for suffix in (".meta.json", ".jsonl"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base[len("agent-"):] if base.startswith("agent-") else base


def describe_agent_meta(meta: dict, agent_id: str = "") -> dict:
    """Normalize an ``agent-<id>.meta.json`` payload into villager facts."""
    if not isinstance(meta, dict):
        meta = {}
    agent_type = meta.get("agentType")
    description = meta.get("description")
    phase = meta.get("workflowPhase")
    depth = meta.get("spawnDepth")
    return {
        "agent_id": agent_id,
        "agent_type": agent_type if isinstance(agent_type, str) else "unknown",
        "description": description if isinstance(description, str) else "",
        "phase": phase if isinstance(phase, str) else "",
        "depth": int(depth) if isinstance(depth, (int, float)) else 0,
        "foreground": meta.get("requestShape") == "foreground",
    }


def split_agent_label(description: str) -> Tuple[str, str]:
    """Split a workflow label like ``verify:mechanism:The held frame``.

    Returns ``(stage, detail)``; the stage is the first colon-separated token when
    it looks like a short tag, otherwise the whole string is the detail.
    """
    if not isinstance(description, str) or ":" not in description:
        return "", (description or "")
    head, rest = description.split(":", 1)
    if 0 < len(head) <= 24 and " " not in head:
        return head, rest
    return "", description


# --------------------------------------------------------------------------
# Workflow state (workflows/wf_<id>.json and journal.jsonl)
# --------------------------------------------------------------------------

def describe_workflow(state: dict) -> dict:
    """Normalize a ``workflows/wf_<id>.json`` payload."""
    if not isinstance(state, dict):
        state = {}
    phases = []
    raw_phases = state.get("phases")
    if isinstance(raw_phases, list):
        for entry in raw_phases:
            if isinstance(entry, dict) and isinstance(entry.get("title"), str):
                phases.append({"title": entry["title"],
                               "detail": entry.get("detail") if isinstance(entry.get("detail"), str) else ""})
    started = state.get("startTime")
    return {
        "run_id": state.get("runId") if isinstance(state.get("runId"), str) else "",
        "name": state.get("workflowName") if isinstance(state.get("workflowName"), str) else "workflow",
        "status": state.get("status") if isinstance(state.get("status"), str) else "",
        "phases": phases,
        "agent_count": int(state["agentCount"]) if isinstance(state.get("agentCount"), (int, float)) else 0,
        "duration_ms": int(state["durationMs"]) if isinstance(state.get("durationMs"), (int, float)) else 0,
        "started_at": float(started) / 1000.0 if isinstance(started, (int, float)) and started > 1e11 else (float(started) if isinstance(started, (int, float)) else 0.0),
        "summary": state.get("summary") if isinstance(state.get("summary"), str) else "",
    }


def journal_entries(records: Iterable[dict]) -> List[dict]:
    """Normalize ``journal.jsonl`` rows into agent lifecycle facts.

    Rows are ``{"type": "started"|"result"|"launched", "agentId", "label", "phase"}``.
    Note the journal is written at start and finish only — live progress must come
    from the agent's own transcript, not from here.
    """
    out: List[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        if kind not in ("started", "result", "launched"):
            continue
        out.append({
            "kind": kind,
            "agent_id": record.get("agentId") if isinstance(record.get("agentId"), str) else "",
            "label": record.get("label") if isinstance(record.get("label"), str) else "",
            "phase": record.get("phase") if isinstance(record.get("phase"), str) else "",
        })
    return out
