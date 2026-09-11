"""Tests for the village world model.

Records here mirror real transcript shapes. The world must be total: any record,
however malformed, produces events or nothing, but never an exception — a parser
crash would freeze the whole view.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from codeville.world import (  # noqa: E402
    DONE,
    FAILED,
    GONE_AFTER,
    IDLE,
    IDLE_AFTER,
    IDLE_AFTER_WITH_TOOL,
    SESSION_GONE_AFTER,
    THINKING,
    WORKING,
    World,
    now,
)

VILLAGE = "-home-ali-project"
SESSION = "sess-1"


def assistant(*blocks, **extra):
    rec = {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}
    rec.update(extra)
    return rec


def tool_use(name="Bash", tid="toolu_1", **inputs):
    return {"type": "tool_use", "id": tid, "name": name, "input": inputs}


def tool_result(tid="toolu_1", content="ok", is_error=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def kinds(events):
    return [e["t"] for e in events]


class WorldTestCase(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def feed(self, record, village=VILLAGE, session=SESSION):
        return self.world.ingest_session_record(village, session, record)

    def mayor(self, village=VILLAGE, session=SESSION):
        return self.world.session(village, session).villagers[
            self.world.session(village, session).main_id]


class TestVillages(WorldTestCase):
    def test_upsert_creates_then_updates(self):
        events = self.world.upsert_village(VILLAGE, name="project", path="/p", exists=True)
        self.assertEqual(kinds(events), ["village.upsert"])
        self.assertEqual(self.world.villages[VILLAGE].name, "project")

        self.assertEqual(self.world.upsert_village(VILLAGE, name="project", path="/p",
                                                   exists=True), [],
                         "an unchanged upsert should emit nothing")
        self.assertEqual(kinds(self.world.upsert_village(VILLAGE, name="renamed")),
                         ["village.upsert"])

    def test_village_created_on_demand(self):
        self.assertEqual(self.world.village("brand-new").slug, "brand-new")

    def test_busy_count_ignores_finished(self):
        self.feed(assistant(tool_use()))
        village = self.world.villages[VILLAGE]
        self.assertEqual(village.busy_count, 1)
        self.mayor().state = DONE
        self.assertEqual(village.busy_count, 0)


class TestMayorLifecycle(WorldTestCase):
    def test_first_record_spawns_the_mayor(self):
        events = self.feed(assistant({"type": "text", "text": "hello"}))
        self.assertIn("agent.spawn", kinds(events))
        self.assertEqual(self.mayor().agent_type, "mayor")

    def test_mayor_spawns_only_once(self):
        self.feed(assistant({"type": "text", "text": "one"}))
        self.assertNotIn("agent.spawn", kinds(self.feed(assistant({"type": "text", "text": "two"}))))

    def test_text_makes_the_mayor_speak_and_think(self):
        self.feed(assistant({"type": "text", "text": "checking"}))
        events = self.feed(assistant({"type": "text", "text": "still checking"}))
        self.assertIn("agent.say", kinds(events))
        self.assertEqual(self.mayor().state, THINKING)

    def test_cwd_and_branch_are_recorded(self):
        self.feed(assistant({"type": "text", "text": "x"}, cwd="/repo", gitBranch="main"))
        session = self.world.session(VILLAGE, SESSION)
        self.assertEqual(session.cwd, "/repo")
        self.assertEqual(session.branch, "main")

    def test_model_and_tokens_accumulate(self):
        for n, mid in enumerate(("msg_a", "msg_b")):
            rec = assistant({"type": "text", "text": "x"})
            rec["message"]["id"] = mid
            rec["message"]["model"] = "claude-opus-5"
            rec["message"]["usage"] = {"output_tokens": 120}
            self.feed(rec)
        session = self.world.session(VILLAGE, SESSION)
        self.assertEqual(session.model, "claude-opus-5")
        self.assertEqual(session.tokens_out, 240)

    def test_usage_is_counted_once_per_api_message(self):
        """One API message is written as one JSONL line per content block, each
        repeating the same usage. Counting per line inflated the real total by
        about 2.7x."""
        for block in ({"type": "text", "text": "thinking out loud"},
                      tool_use(tid="t1"), tool_use(tid="t2")):
            rec = assistant(block)
            rec["message"]["id"] = "msg_same"
            rec["message"]["usage"] = {"output_tokens": 500}
            self.feed(rec)
        self.assertEqual(self.world.session(VILLAGE, SESSION).tokens_out, 500)

    def test_records_without_a_message_id_still_count(self):
        rec = assistant({"type": "text", "text": "x"})
        rec["message"]["usage"] = {"output_tokens": 70}
        self.feed(rec)
        self.feed(rec)
        self.assertEqual(self.world.session(VILLAGE, SESSION).tokens_out, 140)

    def test_synthetic_error_banners_are_not_turns(self):
        """`<synthetic>` records are local banners ("you've hit your limit"),
        not API turns."""
        rec = assistant({"type": "text", "text": "You've hit your weekly limit"})
        rec["message"]["model"] = "<synthetic>"
        rec["message"]["id"] = "msg_syn"
        rec["message"]["usage"] = {"output_tokens": 9999}
        self.feed(assistant({"type": "text", "text": "real turn"}))
        before = self.mayor().say
        self.assertEqual(self.feed(rec), [])
        self.assertEqual(self.world.session(VILLAGE, SESSION).tokens_out, 0)
        self.assertEqual(self.mayor().say, before, "a banner must not become speech")

    def test_old_records_do_not_look_live(self):
        """A session that last wrote hours ago must not appear to be working.

        last_seen starts as a placeholder of "now"; if the first real record is
        max()'d against that placeholder the placeholder wins and a dead session
        stays lit. One on this machine had been dead 3.3 hours, its process gone,
        and Codeville still drew its mayor hammering away at a terminal.
        """
        self.feed(assistant(tool_use(description="a long-finished call"),
                            timestamp="2020-01-01T00:00:00Z"))
        session = self.world.session(VILLAGE, SESSION)
        villager = session.villagers[session.main_id]
        self.assertLess(villager.last_seen, now() - 3600,
                        "the record's own timestamp must win over the placeholder")

        events = self.world.sweep()
        self.assertIn("agent.despawn", kinds(events))
        self.assertNotIn(session.main_id, session.villagers)

    def test_recent_quiet_agent_dozes_rather_than_leaving(self):
        """Quiet for a minute is idle; quiet for hours is gone."""
        self.feed(assistant({"type": "text", "text": "hi"}))
        self.world.sweep(now() + IDLE_AFTER + 1)
        session = self.world.session(VILLAGE, SESSION)
        self.assertEqual(session.villagers[session.main_id].state, IDLE)

    def test_out_of_order_records_do_not_rewind_last_seen(self):
        """Lines are not sorted by timestamp; backward jumps of hours occur. A
        stale record must not make a live agent look quiet."""
        self.feed(assistant({"type": "text", "text": "now"},
                            timestamp="2026-09-11T12:00:00Z"))
        fresh = self.mayor().last_seen
        self.feed(assistant({"type": "text", "text": "stale"},
                            timestamp="2026-09-11T09:00:00Z"))
        self.assertEqual(self.mayor().last_seen, fresh)

    def test_ai_title_updates_the_session(self):
        events = self.feed({"type": "ai-title", "aiTitle": "Refactor the parser"})
        self.assertEqual(kinds(events), ["session.update"])
        self.assertEqual(self.world.session(VILLAGE, SESSION).title, "Refactor the parser")
        self.assertEqual(self.feed({"type": "ai-title", "aiTitle": "Refactor the parser"}), [],
                         "an unchanged title should emit nothing")

    def test_sidechain_records_are_left_to_the_agent_tailer(self):
        self.assertEqual(self.feed(assistant(tool_use(), isSidechain=True)), [])

    def test_noise_records_are_ignored(self):
        for noisy in ("queue-operation", "attachment", "mode", "atis-latch"):
            self.assertEqual(self.feed({"type": noisy}), [], noisy)

    def test_junk_never_raises(self):
        for junk in (None, "string", 42, [], {}, {"type": None}, {"message": 7}):
            self.assertEqual(self.feed(junk), [], repr(junk))


class TestToolFlow(WorldTestCase):
    def test_tool_use_sets_working_and_emits_once(self):
        events = self.feed(assistant(tool_use(description="Run the tests")))
        self.assertIn("agent.tool", kinds(events))
        self.assertIn("agent.state", kinds(events))
        self.assertEqual(self.mayor().state, WORKING)
        self.assertEqual(self.mayor().tool["label"], "Run the tests")

    def test_state_event_only_fires_on_a_real_transition(self):
        self.feed(assistant(tool_use(tid="t1")))
        events = self.feed(assistant(tool_use(tid="t2")))
        self.assertIn("agent.tool", kinds(events))
        self.assertNotIn("agent.state", kinds(events),
                         "already working — re-announcing it is pure noise")

    def test_tool_result_clears_the_tool_and_returns_to_thinking(self):
        self.feed(assistant(tool_use()))
        events = self.feed(tool_result())
        self.assertIn("agent.tool_end", kinds(events))
        self.assertTrue(all(e.get("ok", True) for e in events if e["t"] == "agent.tool_end"))
        self.assertIsNone(self.mayor().tool)
        self.assertEqual(self.mayor().state, THINKING)

    def test_failed_tool_result_is_counted(self):
        self.feed(assistant(tool_use()))
        events = self.feed(tool_result(content="Error: boom", is_error=True))
        end = [e for e in events if e["t"] == "agent.tool_end"][0]
        self.assertFalse(end["ok"])
        self.assertEqual(self.mayor().error_count, 1)

    def test_tool_counts_roll_up_to_the_session(self):
        for i in range(3):
            self.feed(assistant(tool_use(tid=f"t{i}")))
        self.assertEqual(self.world.session(VILLAGE, SESSION).tool_count, 3)
        self.assertEqual(self.mayor().tool_count, 3)


class TestSubagents(WorldTestCase):
    META = {
        "agentType": "workflow-subagent",
        "description": "design:animation-choreography",
        "workflowPhase": "Design",
        "spawnDepth": 1,
        "requestShape": "foreground",
    }

    def test_meta_spawns_a_villager_with_phase_and_stage(self):
        events = self.world.ingest_agent_meta(VILLAGE, SESSION, "a1", self.META)
        self.assertEqual(kinds(events), ["agent.spawn"])
        villager = self.world.session(VILLAGE, SESSION).villagers["a1"]
        self.assertEqual(villager.agent_type, "workflow-subagent")
        self.assertEqual(villager.phase, "Design")
        self.assertEqual(villager.stage, "design")

    def test_meta_reingest_is_quiet_then_updates_on_change(self):
        self.world.ingest_agent_meta(VILLAGE, SESSION, "a1", self.META)
        self.assertEqual(self.world.ingest_agent_meta(VILLAGE, SESSION, "a1", self.META), [])
        changed = dict(self.META, workflowPhase="Verify")
        self.assertEqual(kinds(self.world.ingest_agent_meta(VILLAGE, SESSION, "a1", changed)),
                         ["agent.update"])

    def test_agent_record_spawns_when_meta_has_not_arrived(self):
        events = self.world.ingest_agent_record(VILLAGE, SESSION, "a9",
                                                assistant(tool_use()))
        self.assertIn("agent.spawn", kinds(events))
        self.assertIn("agent.tool", kinds(events))

    def test_finish_agent_marks_done_once(self):
        self.world.ingest_agent_meta(VILLAGE, SESSION, "a1", self.META)
        self.assertEqual(kinds(self.world.finish_agent(VILLAGE, SESSION, "a1", ok=True)),
                         ["agent.done"])
        self.assertEqual(self.world.session(VILLAGE, SESSION).villagers["a1"].state, DONE)
        self.assertEqual(self.world.finish_agent(VILLAGE, SESSION, "a1"), [],
                         "finishing twice should be silent")

    def test_finish_unknown_agent_is_safe(self):
        self.assertEqual(self.world.finish_agent(VILLAGE, SESSION, "nope"), [])

    def test_failed_agent(self):
        self.world.ingest_agent_meta(VILLAGE, SESSION, "a1", self.META)
        self.world.finish_agent(VILLAGE, SESSION, "a1", ok=False)
        self.assertEqual(self.world.session(VILLAGE, SESSION).villagers["a1"].state, FAILED)


class TestWorkflows(WorldTestCase):
    STATE = {
        "runId": "wf_1", "workflowName": "codeville-recon", "status": "running",
        "agentCount": 4, "startTime": 1789128000000,
        "phases": [{"title": "Recon"}, {"title": "Design"}],
    }

    def test_start_then_status_change(self):
        self.assertEqual(kinds(self.world.ingest_workflow(VILLAGE, SESSION, self.STATE)),
                         ["workflow.start"])
        self.assertEqual(self.world.ingest_workflow(VILLAGE, SESSION, self.STATE), [],
                         "no status change, no event")
        finished = dict(self.STATE, status="completed")
        self.assertEqual(kinds(self.world.ingest_workflow(VILLAGE, SESSION, finished)),
                         ["workflow.update"])

    def test_workflow_without_run_id_is_ignored(self):
        self.assertEqual(self.world.ingest_workflow(VILLAGE, SESSION, {"status": "x"}), [])

    def test_journal_started_and_result(self):
        entries = [
            {"kind": "started", "agent_id": "a1", "label": "recon:x", "phase": "Recon"},
            {"kind": "result", "agent_id": "a1", "label": "", "phase": ""},
        ]
        events = self.world.ingest_journal(VILLAGE, SESSION, entries)
        self.assertEqual(kinds(events), ["agent.spawn", "agent.done"])
        villager = self.world.session(VILLAGE, SESSION).villagers["a1"]
        self.assertEqual(villager.phase, "Recon")
        self.assertEqual(villager.state, DONE)

    def test_journal_failed_marks_the_agent_failed(self):
        self.world.ingest_journal(VILLAGE, SESSION, [
            {"kind": "started", "agent_id": "a1", "label": "x", "phase": "Find"}])
        events = self.world.ingest_journal(VILLAGE, SESSION, [
            {"kind": "failed", "agent_id": "a1", "label": "", "phase": ""}])
        self.assertEqual(kinds(events), ["agent.done"])
        self.assertFalse(events[0]["ok"])
        villager = self.world.session(VILLAGE, SESSION).villagers["a1"]
        self.assertEqual(villager.state, FAILED)
        self.assertFalse(villager.awaiting_result)

    def test_journal_entry_without_agent_id_is_skipped(self):
        self.assertEqual(
            self.world.ingest_journal(VILLAGE, SESSION,
                                      [{"kind": "started", "agent_id": "", "label": "x"}]), [])


class TestSweep(WorldTestCase):
    def test_quiet_villager_dozes(self):
        self.feed(assistant({"type": "text", "text": "hi"}))
        events = self.world.sweep(now() + IDLE_AFTER + 1)
        self.assertIn("agent.state", kinds(events))
        self.assertEqual(self.mayor().state, IDLE)

    def test_sweeping_twice_does_not_re_announce(self):
        self.feed(assistant({"type": "text", "text": "hi"}))
        self.world.sweep(now() + IDLE_AFTER + 1)
        self.assertEqual(
            [e for e in self.world.sweep(now() + IDLE_AFTER + 2) if e["t"] == "agent.state"],
            [])

    def test_villager_with_a_tool_in_flight_keeps_working(self):
        """A long Bash call writes nothing for minutes; that agent is the busiest
        one there is, and must not be shown dozing."""
        self.feed(assistant(tool_use(description="Run the full suite")))
        self.assertEqual(self.world.sweep(now() + IDLE_AFTER + 5), [])
        self.assertEqual(self.mayor().state, WORKING)

        # It does eventually give up, so a crashed tool cannot pin it forever.
        self.world.sweep(now() + IDLE_AFTER_WITH_TOOL + 5)
        self.assertEqual(self.mayor().state, IDLE)

    def test_long_quiet_villager_despawns(self):
        self.feed(assistant({"type": "text", "text": "hi"}))
        events = self.world.sweep(now() + GONE_AFTER + 1)
        self.assertIn("agent.despawn", kinds(events))
        self.assertEqual(self.world.session(VILLAGE, SESSION).villagers, {})

    def test_long_quiet_session_ends(self):
        self.feed(assistant({"type": "text", "text": "hi"}))
        events = self.world.sweep(now() + SESSION_GONE_AFTER + 1)
        self.assertIn("session.end", kinds(events))
        self.assertEqual(self.world.villages[VILLAGE].sessions, {})

    def test_agent_the_journal_says_is_alive_thinks_rather_than_dozes(self):
        """A transcript is only appended when a turn finishes, so an agent
        composing a long answer writes nothing for minutes. The journal knows it
        started and never returned, so it must not be shown dozing."""
        self.world.ingest_journal(VILLAGE, SESSION, [
            {"kind": "started", "agent_id": "a1", "label": "design:characters",
             "phase": "Design"}])
        self.world.sweep(now() + IDLE_AFTER + 5)
        villager = self.world.session(VILLAGE, SESSION).villagers["a1"]
        self.assertEqual(villager.state, THINKING)

    def test_agent_the_journal_says_is_alive_is_never_retired(self):
        self.world.ingest_journal(VILLAGE, SESSION, [
            {"kind": "started", "agent_id": "a1", "label": "x", "phase": "Design"}])
        self.world.sweep(now() + GONE_AFTER + 5)
        self.assertIn("a1", self.world.session(VILLAGE, SESSION).villagers)

    def test_journal_result_releases_the_agent(self):
        self.world.ingest_journal(VILLAGE, SESSION, [
            {"kind": "started", "agent_id": "a1", "label": "x", "phase": "Design"}])
        self.world.ingest_journal(VILLAGE, SESSION, [
            {"kind": "result", "agent_id": "a1", "label": "", "phase": ""}])
        villager = self.world.session(VILLAGE, SESSION).villagers["a1"]
        self.assertFalse(villager.awaiting_result)
        self.assertEqual(villager.state, DONE)

    def test_agent_without_a_journal_still_dozes(self):
        """Plain subagents have no journal, so silence still means idle there."""
        self.world.ingest_agent_meta(VILLAGE, SESSION, "a2", {"agentType": "Explore"})
        self.world.sweep(now() + IDLE_AFTER + 5)
        self.assertEqual(self.world.session(VILLAGE, SESSION).villagers["a2"].state, IDLE)

    def test_finished_villagers_are_not_re_idled(self):
        self.world.ingest_agent_meta(VILLAGE, SESSION, "a1", {"agentType": "x"})
        self.world.finish_agent(VILLAGE, SESSION, "a1")
        states = [e for e in self.world.sweep(now() + IDLE_AFTER + 1)
                  if e["t"] == "agent.state"]
        self.assertEqual(states, [])


class TestSnapshot(WorldTestCase):
    def test_snapshot_is_json_serializable_and_sorted(self):
        import json
        self.world.upsert_village("old", name="old", last_active=100)
        self.world.upsert_village("new", name="new", last_active=200)
        snapshot = self.world.snapshot()
        json.dumps(snapshot)  # must not raise
        self.assertEqual([v["name"] for v in snapshot["villages"]][:2], ["new", "old"])

    def test_stats_count_work(self):
        self.feed(assistant(tool_use()))
        self.world.ingest_agent_meta(VILLAGE, SESSION, "a1", {"agentType": "Explore"})
        stats = self.world.stats()
        self.assertEqual(stats["agents_working"], 1)
        self.assertEqual(stats["agents"], 2)
        self.assertEqual(stats["tool_calls"], 1)
        self.assertEqual(stats["villages_live"], 1)

    def test_empty_world_stats(self):
        self.assertEqual(self.world.stats()["villages"], 0)
        self.assertEqual(self.world.stats()["agents"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
