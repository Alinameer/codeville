"""Tests for discovery, tailing and the language/biome mapping.

These build a fake ~/.claude/projects tree so the watcher can be driven end to end
without touching the real one.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from codeville.projects import Project  # noqa: E402
from codeville.watcher import (  # noqa: E402
    LANGUAGE_BIOMES,
    Watcher,
    biome_for,
    detect_language,
)
from codeville.world import World  # noqa: E402


def slugify(path):
    out = path
    for ch in ("/", ".", "_"):
        out = out.replace(ch, "-")
    return out


class WatcherTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="codeville-watch-")
        self.store = os.path.join(self.tmp, "projects")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.store)
        os.makedirs(self.work)
        self.world = World()
        self.emitted = []
        self.watcher = Watcher(self.world, projects_dir=self.store,
                               emit=self.emitted.extend)

    def tearDown(self):
        self.watcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_session(self, name="repo", session_id="sess-1"):
        """Create a project with one transcript, and return its paths."""
        real = os.path.join(self.work, name)
        os.makedirs(real, exist_ok=True)
        store_dir = os.path.join(self.store, slugify(real))
        os.makedirs(store_dir, exist_ok=True)
        transcript = os.path.join(store_dir, f"{session_id}.jsonl")
        with open(transcript, "w") as fh:
            fh.write(json.dumps({"type": "user", "cwd": real, "sessionId": session_id}) + "\n")
        return real, store_dir, transcript

    def append(self, path, *records):
        with open(path, "a") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    @staticmethod
    def assistant(*blocks):
        return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}

    @staticmethod
    def tool_use(name="Bash", tid="t1", **inputs):
        return {"type": "tool_use", "id": tid, "name": name, "input": inputs}


class TestDiscovery(WatcherTestCase):
    def test_discovers_a_project_as_a_village(self):
        real, _, _ = self.make_session()
        self.watcher.discover()
        self.assertIn(slugify(real), self.world.villages)
        self.assertEqual(self.world.villages[slugify(real)].path, real)

    def test_tracks_the_transcript(self):
        _, _, transcript = self.make_session()
        self.watcher.discover()
        self.assertIn(transcript, self.watcher.tailer.tracked)

    def test_dormant_transcripts_are_discovered_but_not_followed(self):
        _, _, transcript = self.make_session()
        old = time.time() - (7 * 3600)
        os.utime(transcript, (old, old))
        self.watcher.discover()
        self.assertNotIn(transcript, self.watcher.tailer.tracked)
        # The village still exists so you can see the repo is there.
        self.assertTrue(self.world.villages)

    def test_new_session_appearing_later_is_picked_up(self):
        self.make_session()
        self.watcher.prime()
        _, _, second = self.make_session(session_id="sess-2")
        self.watcher.discover()
        self.assertIn(second, self.watcher.tailer.tracked)

    def test_missing_projects_dir_is_survivable(self):
        w = Watcher(World(), projects_dir=os.path.join(self.tmp, "absent"))
        self.assertEqual(w.discover(), [])


class TestDraining(WatcherTestCase):
    def test_new_records_become_events(self):
        _, _, transcript = self.make_session()
        self.watcher.prime()
        self.append(transcript, self.assistant(self.tool_use(description="Build it")))
        events = self.watcher.drain()
        self.assertIn("agent.tool", [e["t"] for e in events])

    def test_priming_does_not_replay_history_as_live(self):
        _, _, transcript = self.make_session()
        self.append(transcript, *[self.assistant(self.tool_use(tid=f"t{i}"))
                                  for i in range(20)])
        self.watcher.prime()
        self.assertEqual(self.watcher.drain(), [],
                         "history should already be consumed by priming")

    def test_subagent_log_is_discovered_and_tailed(self):
        _, store_dir, _ = self.make_session()
        self.watcher.prime()

        agents = os.path.join(store_dir, "sess-1", "subagents")
        os.makedirs(agents)
        meta = os.path.join(agents, "agent-a1.meta.json")
        with open(meta, "w") as fh:
            json.dump({"agentType": "Explore", "description": "look around",
                       "workflowPhase": "Recon"}, fh)
        log = os.path.join(agents, "agent-a1.jsonl")
        with open(log, "w") as fh:
            fh.write(json.dumps(self.assistant(self.tool_use(name="Grep", pattern="TODO"))) + "\n")

        self.watcher.discover()
        events = self.watcher.drain()
        types = [e["t"] for e in events]
        self.assertIn("agent.spawn", types)
        villagers = list(self.world.villages.values())[0].sessions["sess-1"].villagers
        self.assertIn("a1", villagers)
        self.assertEqual(villagers["a1"].agent_type, "Explore")
        self.assertEqual(villagers["a1"].phase, "Recon")

    def test_workflow_state_file_is_read(self):
        _, store_dir, _ = self.make_session()
        self.watcher.prime()
        workflows = os.path.join(store_dir, "sess-1", "workflows")
        os.makedirs(workflows)
        with open(os.path.join(workflows, "wf_abc.json"), "w") as fh:
            json.dump({"runId": "wf_abc", "workflowName": "review", "status": "running",
                       "agentCount": 3, "phases": [{"title": "Find"}]}, fh)
        self.watcher.discover()
        events = self.watcher.drain()
        self.assertIn("workflow.start", [e["t"] for e in events])

    def test_journal_drives_agent_completion(self):
        _, store_dir, _ = self.make_session()
        self.watcher.prime()
        wf = os.path.join(store_dir, "sess-1", "subagents", "workflows", "wf_abc")
        os.makedirs(wf)
        with open(os.path.join(wf, "journal.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "started", "agentId": "a7",
                                 "label": "find:bugs", "phase": "Find"}) + "\n")
            fh.write(json.dumps({"type": "result", "agentId": "a7"}) + "\n")
        self.watcher.discover()
        types = [e["t"] for e in self.watcher.drain()]
        self.assertIn("agent.spawn", types)
        self.assertIn("agent.done", types)

    def test_tool_results_directory_is_skipped(self):
        """tool-results holds large raw payloads that are never rendered."""
        _, store_dir, _ = self.make_session()
        results = os.path.join(store_dir, "sess-1", "tool-results")
        os.makedirs(results)
        with open(os.path.join(results, "agent-huge.jsonl"), "w") as fh:
            fh.write("{}\n")
        self.watcher.prime()
        self.assertFalse([p for p in self.watcher.tailer.tracked if "tool-results" in p])

    def test_malformed_records_do_not_stop_the_stream(self):
        _, _, transcript = self.make_session()
        self.watcher.prime()
        with open(transcript, "a") as fh:
            fh.write("not json at all\n")
            fh.write(json.dumps(self.assistant(self.tool_use(description="ok"))) + "\n")
        self.assertIn("agent.tool", [e["t"] for e in self.watcher.drain()])


class TestTick(WatcherTestCase):
    def test_tick_emits_through_the_callback(self):
        _, _, transcript = self.make_session()
        self.watcher.prime()
        self.append(transcript, self.assistant(self.tool_use(description="Ship it")))
        self.watcher.tick()
        self.assertIn("agent.tool", [e["t"] for e in self.emitted])

    def test_poll_loop_survives_a_failing_tick(self):
        """A watcher thread that dies would silently freeze the whole village,
        so the poll loop must keep going when a tick raises."""
        self.make_session()
        self.watcher.prime()

        calls = []

        def exploding_tick():
            calls.append(1)
            raise RuntimeError("tick blew up")

        self.watcher.tick = exploding_tick
        self.watcher.poll_interval = 0.02
        self.watcher.start()
        deadline = time.time() + 3
        while len(calls) < 3 and time.time() < deadline:
            time.sleep(0.02)
        thread = self.watcher._thread
        self.watcher.stop()

        self.assertGreaterEqual(len(calls), 3, "loop stopped after the first failure")
        self.assertIsNotNone(thread)

    def test_stop_is_idempotent(self):
        self.watcher.start()
        self.watcher.stop()
        self.watcher.stop()  # must not raise
        self.assertIsNone(self.watcher._thread)


class TestLanguageAndBiome(WatcherTestCase):
    def write(self, *names):
        target = os.path.join(self.work, "langrepo")
        os.makedirs(target, exist_ok=True)
        for name in names:
            with open(os.path.join(target, name), "w") as fh:
                fh.write("x")
        return target

    def test_detects_dominant_language(self):
        target = self.write("a.py", "b.py", "c.py", "d.js")
        self.assertEqual(detect_language(target), "python")

    def test_ignores_vendor_directories(self):
        target = self.write("only.py")
        noise = os.path.join(target, "node_modules")
        os.makedirs(noise, exist_ok=True)
        for i in range(50):
            with open(os.path.join(noise, f"x{i}.js"), "w") as fh:
                fh.write("x")
        self.assertEqual(detect_language(target), "python")

    def test_unknown_directory_returns_empty(self):
        self.assertEqual(detect_language("/nonexistent/xyzzy"), "")
        self.assertEqual(detect_language(""), "")

    def test_biome_follows_language(self):
        target = self.write("main.rs")
        project = Project(slug="-x", store_dir="/s", path=target)
        self.assertEqual(biome_for(project), LANGUAGE_BIOMES["rust"])

    def test_biome_is_stable_without_a_language(self):
        project = Project(slug="-home-ali-notes", store_dir="/s", path="")
        first = biome_for(project)
        self.assertEqual(first, biome_for(project))
        self.assertIn(first, set(LANGUAGE_BIOMES.values()))

    def test_different_slugs_can_differ(self):
        biomes = {biome_for(Project(slug=f"-repo-{i}", store_dir="/s", path=""))
                  for i in range(12)}
        self.assertGreater(len(biomes), 1, "hash spread should not collapse to one biome")


if __name__ == "__main__":
    unittest.main(verbosity=2)
