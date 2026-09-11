"""The village world: live state assembled from transcript records.

This is the model the UI draws. It holds villages (projects), the expeditions
running in them (sessions), and the villagers doing the work (the main thread plus
every subagent). Feeding it a record returns the list of events to broadcast, so
the daemon never has to diff state or re-send everything.

Two rules shape the design:

* **Never trust the input.** Records come from files written by another process
  and may be truncated, reordered or of an unknown type. Every ingest path is
  total: unknown shapes produce no events rather than an exception.
* **Idle is not ended.** A transcript goes quiet both when Claude is thinking and
  when the user walked away, and nothing is written to say which. So a villager
  goes ``idle`` after :data:`IDLE_AFTER` seconds of silence and is only retired
  after :data:`GONE_AFTER`, rather than being deleted the moment writes stop.
* **The journal is the liveness oracle.** A transcript is only appended to when a
  turn *finishes*, so an agent spending three minutes composing a long answer
  writes nothing at all in the meantime — by file activity alone it is
  indistinguishable from one that died. A workflow journal records ``started``
  and ``result`` per agent, so an agent with a start and no result is known to be
  alive however quiet its file is, and is shown thinking rather than dozing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

from .records import (
    describe_agent_meta,
    is_synthetic,
    message_id,
    describe_tool_use,
    describe_workflow,
    message_text,
    model_name,
    record_time,
    record_type,
    split_agent_label,
    tool_result_is_error,
    tool_results,
    tool_uses,
    usage,
)

#: Seconds of silence after which a villager is shown dozing rather than working.
IDLE_AFTER = 45.0

#: The same, but for a villager with a tool still in flight. A single Bash call can
#: run for minutes without writing anything to the transcript, and showing that
#: agent as dozing is simply wrong — it is the one doing the most work.
IDLE_AFTER_WITH_TOOL = 12 * 60.0

#: Seconds of silence after which a villager leaves the village entirely.
GONE_AFTER = 15 * 60.0

#: Seconds of silence after which a session is considered finished.
SESSION_GONE_AFTER = 30 * 60.0

#: Villager lifecycle states, mirrored by the UI's animation state machine.
SPAWNING, WORKING, THINKING, IDLE, DONE, FAILED = (
    "spawning", "working", "thinking", "idle", "done", "failed")

#: The main conversation thread is drawn as the village's mayor.
MAYOR_AGENT_TYPE = "mayor"


def now() -> float:
    return time.time()


@dataclass
class Villager:
    """One worker: the main thread, a subagent, or a workflow agent."""

    id: str
    session_id: str
    agent_type: str = MAYOR_AGENT_TYPE
    description: str = ""
    phase: str = ""
    depth: int = 0
    state: str = SPAWNING
    #: The tool currently running, as produced by describe_tool_use.
    tool: Optional[dict] = None
    say: str = ""
    started_at: float = field(default_factory=now)
    last_seen: float = field(default_factory=now)
    #: False until a record with a real timestamp has been applied. Until then
    #: last_seen is only a placeholder and must be overwritten rather than
    #: max()'d — otherwise a transcript whose records are hours old keeps the
    #: creation time and a long-dead session shows as working forever.
    seen_record: bool = False
    ended_at: Optional[float] = None
    ok: Optional[bool] = None
    tool_count: int = 0
    error_count: int = 0
    tokens_out: int = 0
    #: True between a journal "started" and its "result": the agent is known to be
    #: alive, so silence means it is composing, not gone.
    awaiting_result: bool = False

    @property
    def stage(self) -> str:
        """The short tag from a workflow label, e.g. "verify" in "verify:thing"."""
        return split_agent_label(self.description)[0]

    @property
    def is_finished(self) -> bool:
        return self.state in (DONE, FAILED)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session": self.session_id,
            "agent_type": self.agent_type,
            "description": self.description,
            "stage": self.stage,
            "phase": self.phase,
            "depth": self.depth,
            "state": self.state,
            "tool": self.tool,
            "say": self.say,
            "started_at": self.started_at,
            "last_seen": self.last_seen,
            "ended_at": self.ended_at,
            "ok": self.ok,
            "tool_count": self.tool_count,
            "error_count": self.error_count,
            "tokens_out": self.tokens_out,
            "awaiting_result": self.awaiting_result,
        }


@dataclass
class Session:
    """One Claude Code session — an expedition in a village."""

    id: str
    village: str
    title: str = ""
    branch: str = ""
    model: str = ""
    cwd: str = ""
    started_at: float = field(default_factory=now)
    last_activity: float = field(default_factory=now)
    villagers: Dict[str, Villager] = field(default_factory=dict)
    workflows: Dict[str, dict] = field(default_factory=dict)
    tokens_out: int = 0
    tool_count: int = 0
    #: message.ids whose usage has already been counted. One API message spans
    #: many JSONL lines that each repeat the same usage, so counting per line
    #: inflated the token total by ~2.7x on real transcripts.
    counted_messages: Set[str] = field(default_factory=set)

    @property
    def main_id(self) -> str:
        return f"{self.id}:main"

    @property
    def active_villagers(self) -> List[Villager]:
        return [v for v in self.villagers.values() if not v.is_finished]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "village": self.village,
            "title": self.title,
            "branch": self.branch,
            "model": self.model,
            "started_at": self.started_at,
            "last_activity": self.last_activity,
            "tokens_out": self.tokens_out,
            "tool_count": self.tool_count,
            "villagers": [v.to_dict() for v in self.villagers.values()],
            "workflows": list(self.workflows.values()),
        }


@dataclass
class Village:
    """One project directory."""

    slug: str
    name: str = ""
    path: str = ""
    exists: bool = False
    stale: bool = False
    biome: str = "meadow"
    sessions: Dict[str, Session] = field(default_factory=dict)
    last_active: float = 0.0

    @property
    def busy_count(self) -> int:
        return sum(len(s.active_villagers) for s in self.sessions.values())

    def to_dict(self, include_sessions: bool = True) -> dict:
        data = {
            "slug": self.slug,
            "name": self.name,
            "path": self.path,
            "exists": self.exists,
            "stale": self.stale,
            "biome": self.biome,
            "last_active": self.last_active,
            "session_count": len(self.sessions),
            "busy": self.busy_count,
        }
        if include_sessions:
            data["sessions"] = [s.to_dict() for s in self.sessions.values()]
        return data


class World:
    """Live state for every village, and the event stream describing changes."""

    def __init__(self) -> None:
        self.villages: Dict[str, Village] = {}
        self.started_at = now()

    # -- villages ----------------------------------------------------------

    def upsert_village(self, slug: str, **fields) -> List[dict]:
        village = self.villages.get(slug)
        if village is None:
            village = Village(slug=slug, name=fields.get("name") or slug)
            self.villages[slug] = village
        changed = False
        for key, value in fields.items():
            if hasattr(village, key) and getattr(village, key) != value:
                setattr(village, key, value)
                changed = True
        if not changed and slug in self.villages:
            return []
        return [{"t": "village.upsert", "village": village.to_dict(include_sessions=False)}]

    def village(self, slug: str) -> Village:
        if slug not in self.villages:
            self.villages[slug] = Village(slug=slug, name=slug)
        return self.villages[slug]

    # -- sessions ----------------------------------------------------------

    def session(self, village_slug: str, session_id: str) -> Session:
        village = self.village(village_slug)
        session = village.sessions.get(session_id)
        if session is None:
            session = Session(id=session_id, village=village_slug)
            village.sessions[session_id] = session
        return session

    #: Cap on remembered message ids per session, so a long session cannot grow
    #: the set without bound. Duplicate lines for one message always arrive
    #: together, so a small window is enough.
    _COUNTED_WINDOW = 512

    @staticmethod
    def _count_tokens(session: Session, villager: Villager, record: dict) -> None:
        """Add this record's usage, but only once per API message."""
        mid = message_id(record)
        if mid:
            if mid in session.counted_messages:
                return
            session.counted_messages.add(mid)
            if len(session.counted_messages) > World._COUNTED_WINDOW:
                # Drop an arbitrary half; ids are only needed briefly.
                for stale in list(session.counted_messages)[: World._COUNTED_WINDOW // 2]:
                    session.counted_messages.discard(stale)
        out = usage(record).get("output_tokens", 0)
        villager.tokens_out += out
        session.tokens_out += out

    @staticmethod
    def _touch(villager: Villager, stamp: float) -> None:
        """Advance a villager's clock.

        The first real record wins outright; after that time only moves forward,
        because transcript lines are not sorted and backward jumps of hours occur.
        """
        if not villager.seen_record:
            villager.last_seen = stamp
            villager.seen_record = True
        else:
            villager.last_seen = max(villager.last_seen, stamp)

    @staticmethod
    def _set_state(villager: "Villager", state: str) -> List[dict]:
        """Change a villager's state, emitting an event only on a real transition.

        Without this guard a busy agent re-announces "working" on every single
        tool call, which is most of the event volume and tells the UI nothing.
        """
        if villager.state == state:
            return []
        villager.state = state
        return [{"t": "agent.state", "agent": villager.id, "state": state}]

    def _villager(self, session: Session, villager_id: str,
                  **fields) -> tuple[Villager, bool]:
        villager = session.villagers.get(villager_id)
        created = villager is None
        if created:
            villager = Villager(id=villager_id, session_id=session.id, **fields)
            session.villagers[villager_id] = villager
        return villager, created

    # -- ingest: main transcript -------------------------------------------

    def ingest_session_record(self, village_slug: str, session_id: str,
                              record: dict) -> List[dict]:
        """Feed one record from a main session transcript."""
        if not isinstance(record, dict):
            return []
        kind = record_type(record)
        if kind not in ("user", "assistant", "system"):
            return self._ingest_metadata(village_slug, session_id, record)

        # Records produced inside a subagent are handled by the agent tailer.
        if record.get("isSidechain"):
            return []

        session = self.session(village_slug, session_id)
        events: List[dict] = []
        stamp = record_time(record) or now()
        session.last_activity = max(session.last_activity, stamp)
        self.village(village_slug).last_active = session.last_activity

        for key, attr in (("cwd", "cwd"), ("gitBranch", "branch")):
            value = record.get(key)
            if isinstance(value, str) and value and getattr(session, attr) != value:
                setattr(session, attr, value)

        mayor, created = self._villager(session, session.main_id,
                                        agent_type=MAYOR_AGENT_TYPE,
                                        description="the mayor")
        if created:
            events.append({"t": "agent.spawn", "agent": mayor.to_dict()})
        self._touch(mayor, stamp)

        if kind == "assistant":
            if is_synthetic(record):
                # A local error banner, not a real turn.
                return events
            model = model_name(record)
            if model and session.model != model:
                session.model = model
            self._count_tokens(session, mayor, record)
            events += self._apply_assistant(session, mayor, record, stamp)
        elif kind == "user":
            events += self._apply_tool_results(session, mayor, record, stamp)

        return events

    def _ingest_metadata(self, village_slug: str, session_id: str,
                         record: dict) -> List[dict]:
        """Non-conversational records that still carry useful labels."""
        kind = record_type(record)
        if kind != "ai-title":
            return []
        title = record.get("aiTitle")
        if not isinstance(title, str) or not title:
            return []
        session = self.session(village_slug, session_id)
        if session.title == title:
            return []
        session.title = title
        return [{"t": "session.update", "session": session.id,
                 "village": village_slug, "title": title}]

    def _apply_assistant(self, session: Session, villager: Villager,
                         record: dict, stamp: float) -> List[dict]:
        events: List[dict] = []
        calls = tool_uses(record)

        if not calls:
            text = message_text(record, limit=160)
            if text:
                villager.say = text
                events.append({"t": "agent.say", "agent": villager.id, "text": text})
                events += self._set_state(villager, THINKING)
            return events

        for block in calls:
            described = describe_tool_use(block)
            if described is None:
                continue
            villager.tool = described
            villager.tool_count += 1
            session.tool_count += 1
            self._touch(villager, stamp)
            events.append({"t": "agent.tool", "agent": villager.id,
                           "tool": described, "at": stamp})
            events += self._set_state(villager, WORKING)
        return events

    def _apply_tool_results(self, session: Session, villager: Villager,
                            record: dict, stamp: float) -> List[dict]:
        events: List[dict] = []
        for block in tool_results(record):
            failed = tool_result_is_error(block)
            if failed:
                villager.error_count += 1
            events.append({"t": "agent.tool_end", "agent": villager.id,
                           "tool_id": block.get("tool_use_id", ""),
                           "ok": not failed, "at": stamp})
        if events:
            villager.tool = None
            events += self._set_state(villager, THINKING)
        return events

    # -- ingest: subagents -------------------------------------------------

    def ingest_agent_meta(self, village_slug: str, session_id: str, agent_id: str,
                          meta: dict) -> List[dict]:
        """Register (or update) a subagent from its ``agent-<id>.meta.json``."""
        session = self.session(village_slug, session_id)
        facts = describe_agent_meta(meta, agent_id)
        villager, created = self._villager(
            session, agent_id,
            agent_type=facts["agent_type"], description=facts["description"],
            phase=facts["phase"], depth=facts["depth"])
        if created:
            villager.state = SPAWNING
            self.village(village_slug).last_active = max(
                self.village(village_slug).last_active, villager.started_at)
            return [{"t": "agent.spawn", "agent": villager.to_dict()}]

        changed = False
        for attr, value in (("agent_type", facts["agent_type"]),
                            ("description", facts["description"]),
                            ("phase", facts["phase"]),
                            ("depth", facts["depth"])):
            if value and getattr(villager, attr) != value:
                setattr(villager, attr, value)
                changed = True
        return [{"t": "agent.update", "agent": villager.to_dict()}] if changed else []

    def ingest_agent_record(self, village_slug: str, session_id: str, agent_id: str,
                            record: dict) -> List[dict]:
        """Feed one record from a subagent transcript."""
        if not isinstance(record, dict):
            return []
        session = self.session(village_slug, session_id)
        villager, created = self._villager(session, agent_id,
                                           agent_type="subagent")
        events: List[dict] = []
        if created:
            events.append({"t": "agent.spawn", "agent": villager.to_dict()})

        stamp = record_time(record) or now()
        self._touch(villager, stamp)
        session.last_activity = max(session.last_activity, stamp)
        self.village(village_slug).last_active = session.last_activity

        kind = record_type(record)
        if kind == "assistant":
            if is_synthetic(record):
                return events
            self._count_tokens(session, villager, record)
            events += self._apply_assistant(session, villager, record, stamp)
        elif kind == "user":
            events += self._apply_tool_results(session, villager, record, stamp)
        return events

    def finish_agent(self, village_slug: str, session_id: str, agent_id: str,
                     ok: bool = True) -> List[dict]:
        """Mark a subagent finished — driven by the workflow journal."""
        session = self.session(village_slug, session_id)
        villager = session.villagers.get(agent_id)
        if villager is None or villager.is_finished:
            return []
        villager.state = DONE if ok else FAILED
        villager.ok = ok
        villager.awaiting_result = False
        villager.ended_at = now()
        villager.tool = None
        return [{"t": "agent.done", "agent": villager.id, "ok": ok}]

    # -- ingest: workflows -------------------------------------------------

    def ingest_workflow(self, village_slug: str, session_id: str,
                        state: dict) -> List[dict]:
        session = self.session(village_slug, session_id)
        described = describe_workflow(state)
        run_id = described["run_id"]
        if not run_id:
            return []
        previous = session.workflows.get(run_id)
        session.workflows[run_id] = described
        if previous is None:
            return [{"t": "workflow.start", "session": session_id,
                     "village": village_slug, "workflow": described}]
        if previous.get("status") != described["status"]:
            return [{"t": "workflow.update", "session": session_id,
                     "village": village_slug, "workflow": described}]
        return []

    def ingest_journal(self, village_slug: str, session_id: str,
                       entries: Iterable[dict]) -> List[dict]:
        """Apply ``journal.jsonl`` rows: agent started / returned."""
        events: List[dict] = []
        session = self.session(village_slug, session_id)
        for entry in entries:
            agent_id = entry.get("agent_id")
            if not agent_id:
                continue
            if entry["kind"] == "started":
                villager, created = self._villager(
                    session, agent_id, agent_type="workflow-subagent",
                    description=entry.get("label", ""), phase=entry.get("phase", ""))
                villager.awaiting_result = True
                if created:
                    events.append({"t": "agent.spawn", "agent": villager.to_dict()})
                else:
                    if entry.get("label") and villager.description != entry["label"]:
                        villager.description = entry["label"]
                    if entry.get("phase") and villager.phase != entry["phase"]:
                        villager.phase = entry["phase"]
                    events.append({"t": "agent.update", "agent": villager.to_dict()})
            elif entry["kind"] == "result":
                events += self.finish_agent(village_slug, session_id, agent_id, ok=True)
            elif entry["kind"] == "failed":
                events += self.finish_agent(village_slug, session_id, agent_id, ok=False)
        return events

    # -- housekeeping ------------------------------------------------------

    def sweep(self, at: Optional[float] = None) -> List[dict]:
        """Age out quiet villagers and sessions. Call on a timer."""
        moment = at if at is not None else now()
        events: List[dict] = []
        for village in self.villages.values():
            for session in list(village.sessions.values()):
                for villager in list(session.villagers.values()):
                    quiet = moment - villager.last_seen
                    patience = IDLE_AFTER_WITH_TOOL if villager.tool else IDLE_AFTER
                    if not villager.is_finished and quiet > patience:
                        if villager.awaiting_result:
                            # The journal says this agent is still running, so the
                            # silence is a long turn being composed, not absence.
                            events += self._set_state(villager, THINKING)
                        else:
                            if villager.state != IDLE:
                                villager.tool = None
                            events += self._set_state(villager, IDLE)
                    if quiet > GONE_AFTER and not villager.awaiting_result:
                        del session.villagers[villager.id]
                        events.append({"t": "agent.despawn", "agent": villager.id})
                if moment - session.last_activity > SESSION_GONE_AFTER:
                    del village.sessions[session.id]
                    events.append({"t": "session.end", "session": session.id,
                                   "village": village.slug})
        return events

    # -- snapshot ----------------------------------------------------------

    def snapshot(self) -> dict:
        """Full state for a newly connected view."""
        villages = sorted(self.villages.values(),
                          key=lambda v: v.last_active, reverse=True)
        return {
            "server_started": self.started_at,
            "now": now(),
            "villages": [v.to_dict() for v in villages],
            "stats": self.stats(),
        }

    def stats(self) -> dict:
        sessions = [s for v in self.villages.values() for s in v.sessions.values()]
        villagers = [w for s in sessions for w in s.villagers.values()]
        return {
            "villages": len(self.villages),
            "villages_live": sum(1 for v in self.villages.values() if v.busy_count),
            "sessions": len(sessions),
            "agents": len(villagers),
            "agents_working": sum(1 for w in villagers if w.state == WORKING),
            "tool_calls": sum(s.tool_count for s in sessions),
            "tokens_out": sum(s.tokens_out for s in sessions),
        }
