"""Tests for transcript record interpretation.

Fixtures mirror shapes taken from real transcripts. The parser must never raise on
junk: a crash here would freeze the whole village, so malformed input is expected
to degrade to empty values instead.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from codeville.records import (  # noqa: E402
    IGNORED_TYPES,
    agent_id_from_path,
    content_blocks,
    describe_agent_meta,
    describe_tool_use,
    describe_workflow,
    is_sidechain,
    journal_entries,
    message_text,
    model_name,
    parse_timestamp,
    record_time,
    record_type,
    split_agent_label,
    tool_category,
    tool_display_name,
    tool_label,
    tool_result_is_error,
    tool_results,
    tool_uses,
    usage,
)


def assistant(*blocks, **extra):
    rec = {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}
    rec.update(extra)
    return rec


def use(name, **inputs):
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": inputs}


class TestClassification(unittest.TestCase):
    def test_record_type_and_sidechain(self):
        self.assertEqual(record_type({"type": "assistant"}), "assistant")
        self.assertEqual(record_type({}), "")
        self.assertEqual(record_type({"type": 42}), "")
        self.assertTrue(is_sidechain({"isSidechain": True}))
        self.assertFalse(is_sidechain({}))

    def test_known_noise_types_are_listed(self):
        for noisy in ("queue-operation", "ai-title", "attachment", "mode"):
            self.assertIn(noisy, IGNORED_TYPES)


class TestTimestamps(unittest.TestCase):
    def test_iso_with_z(self):
        # 2026-09-11T12:00:00Z, verified against datetime(...).timestamp()
        self.assertAlmostEqual(parse_timestamp("2026-09-11T12:00:00.000Z"),
                               1789128000.0, places=0)

    def test_iso_with_offset(self):
        naive = parse_timestamp("2026-09-11T12:00:00Z")
        offset = parse_timestamp("2026-09-11T15:00:00+03:00")
        self.assertEqual(naive, offset)

    def test_epoch_seconds_and_millis(self):
        self.assertEqual(parse_timestamp(1789041600), 1789041600.0)
        self.assertEqual(parse_timestamp(1789041600000), 1789041600.0)

    def test_garbage_returns_none(self):
        for bad in (None, "", "not a date", [], {}):
            self.assertIsNone(parse_timestamp(bad))

    def test_record_time_reads_the_field(self):
        self.assertIsNotNone(record_time({"timestamp": "2026-09-11T12:00:00Z"}))
        self.assertIsNone(record_time({}))


class TestContent(unittest.TestCase):
    def test_string_content_is_wrapped(self):
        rec = {"type": "user", "message": {"content": "plain string"}}
        self.assertEqual(content_blocks(rec), [{"type": "text", "text": "plain string"}])

    def test_non_dict_blocks_are_dropped(self):
        rec = assistant({"type": "text", "text": "ok"}, "junk", None, 7)
        self.assertEqual(len(content_blocks(rec)), 1)

    def test_missing_message_is_empty(self):
        self.assertEqual(content_blocks({"type": "assistant"}), [])
        self.assertEqual(content_blocks({"message": "not a dict"}), [])

    def test_message_text_joins_and_collapses_whitespace(self):
        rec = assistant({"type": "text", "text": "hello\n\n  world"},
                        {"type": "text", "text": "again"})
        self.assertEqual(message_text(rec), "hello world again")

    def test_message_text_truncates(self):
        rec = assistant({"type": "text", "text": "x" * 1000})
        self.assertEqual(len(message_text(rec, limit=50)), 50)

    def test_thinking_blocks_are_not_shown_as_text(self):
        rec = assistant({"type": "thinking", "thinking": "secret reasoning"},
                        {"type": "text", "text": "visible"})
        self.assertEqual(message_text(rec), "visible")


class TestUsageAndModel(unittest.TestCase):
    def test_usage_extracted(self):
        rec = assistant(**{})
        rec["message"]["usage"] = {"input_tokens": 10, "output_tokens": 5,
                                   "cache_read_input_tokens": 100, "unrelated": "x"}
        self.assertEqual(usage(rec), {"input_tokens": 10, "output_tokens": 5,
                                      "cache_read_input_tokens": 100})

    def test_usage_absent_is_empty(self):
        self.assertEqual(usage(assistant()), {})
        self.assertEqual(usage({}), {})

    def test_model_name(self):
        rec = assistant()
        rec["message"]["model"] = "claude-opus-5"
        self.assertEqual(model_name(rec), "claude-opus-5")
        self.assertEqual(model_name({}), "")


class TestToolUses(unittest.TestCase):
    def test_finds_tool_use_blocks(self):
        rec = assistant({"type": "text", "text": "hi"}, use("Bash", command="ls"))
        self.assertEqual(len(tool_uses(rec)), 1)

    def test_describe_includes_category_and_display(self):
        d = describe_tool_use(use("Bash", description="List files", command="ls -la"))
        self.assertEqual(d["tool"], "Bash")
        self.assertEqual(d["category"], "run")
        self.assertEqual(d["label"], "List files")
        self.assertFalse(d["spawns_agent"])

    def test_describe_rejects_nameless_block(self):
        self.assertIsNone(describe_tool_use({"type": "tool_use", "id": "x"}))

    def test_agent_spawning_tools_are_flagged(self):
        for name in ("Task", "Agent", "Workflow"):
            self.assertTrue(describe_tool_use(use(name, description="go"))["spawns_agent"])
        self.assertFalse(describe_tool_use(use("Read", file_path="/a/b"))["spawns_agent"])

    def test_subagent_type_is_surfaced(self):
        d = describe_tool_use(use("Task", description="dig", subagent_type="Explore"))
        self.assertEqual(d["subagent_type"], "Explore")


class TestToolLabels(unittest.TestCase):
    def test_bash_prefers_description_over_command(self):
        self.assertEqual(tool_label("Bash", {"description": "Build it", "command": "make"}),
                         "Build it")

    def test_bash_without_a_description_shows_only_the_program(self):
        """Never the command body: arguments carry paths, URLs and piped file
        contents, and the README promises they are not rendered."""
        self.assertEqual(tool_label("Bash", {"command": "make all\nmake test"}), "make …")
        self.assertEqual(tool_label("Bash", {"command": "git status --porcelain"}), "git …")
        self.assertEqual(
            tool_label("Bash", {"command": "/usr/local/bin/deploy --prod --token=s3cret"}),
            "deploy …")
        self.assertEqual(tool_label("Bash", {"command": "FOO=bar npm run build"}), "npm …")

    def test_bash_label_never_contains_arguments(self):
        leaky = 'cat google-services.json | python3 -c "import json,sys"'
        label = tool_label("Bash", {"command": leaky})
        for secret in ("google-services", "json", "import", "--", "|"):
            self.assertNotIn(secret, label, f"{secret!r} leaked into {label!r}")

    def test_file_tools_shorten_paths(self):
        for tool in ("Read", "Edit", "Write"):
            self.assertEqual(tool_label(tool, {"file_path": "/very/deep/nested/dir/app.py"}),
                             "dir/app.py")

    def test_todowrite_prefers_in_progress_item(self):
        todos = [{"content": "done thing", "status": "completed"},
                 {"activeForm": "Wiring the tray", "status": "in_progress"}]
        self.assertEqual(tool_label("TodoWrite", {"todos": todos}), "Wiring the tray")

    def test_todowrite_without_items(self):
        self.assertEqual(tool_label("TodoWrite", {"todos": []}), "planning")

    def test_workflow_label_uses_the_fields_that_actually_exist(self):
        """Measured on real transcripts: a Workflow call carries script /
        description / scriptPath. `name` never appears, which left 87% of
        workflow bubbles blank."""
        self.assertEqual(tool_label("Workflow", {"description": "Recon and design",
                                                 "script": "export const meta"}),
                         "Recon and design")
        self.assertEqual(tool_label("Workflow", {"scriptPath": "/x/y/find-flaky-wf_a1.js"}),
                         "find-flaky-wf_a1")
        self.assertEqual(tool_label("Workflow", {"script": "export const meta = {}"}),
                         "running a workflow")

    def test_unknown_tool_uses_generic_fallback(self):
        self.assertEqual(tool_label("Frobnicate", {"description": "doing a thing"}),
                         "doing a thing")

    def test_label_is_truncated_and_single_line(self):
        label = tool_label("WebSearch", {"query": "a" * 500})
        self.assertLessEqual(len(label), 72)

    def test_non_dict_input_is_safe(self):
        self.assertEqual(tool_label("Bash", None), "")
        self.assertEqual(tool_label("Bash", "string"), "")

    def test_empty_input_yields_empty_label(self):
        self.assertEqual(tool_label("Read", {}), "")


class TestToolNaming(unittest.TestCase):
    def test_mcp_names_are_humanized(self):
        cases = {
            "mcp__claude_ai_pletor_mcp__generate_image": "pletor: generate image",
            "mcp__context7__resolve-library-id": "context7: resolve library id",
            "mcp__claude_ai_Google_Drive__search_files": "Google Drive: search files",
        }
        for raw, expected in cases.items():
            self.assertEqual(tool_display_name(raw), expected)

    def test_plain_tools_pass_through(self):
        self.assertEqual(tool_display_name("Bash"), "Bash")

    def test_malformed_mcp_name_does_not_raise(self):
        self.assertEqual(tool_display_name("mcp__"), "mcp__")
        self.assertEqual(tool_display_name(None), "")

    def test_categories(self):
        expected = {"Bash": "run", "Read": "read", "Edit": "edit", "Write": "write",
                    "Grep": "search", "WebFetch": "web", "TodoWrite": "plan",
                    "Task": "summon", "AskUserQuestion": "ask",
                    "mcp__x__y": "mcp", "Frobnicate": "other"}
        for tool, category in expected.items():
            self.assertEqual(tool_category(tool), category, tool)


class TestToolResults(unittest.TestCase):
    def test_finds_result_blocks(self):
        rec = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]}}
        self.assertEqual(len(tool_results(rec)), 1)

    def test_is_error_flag_wins(self):
        self.assertTrue(tool_result_is_error({"is_error": True, "content": "fine"}))

    def test_result_text_is_not_used_to_guess_failure(self):
        """Across the corpus is_error is simply absent on normal successes, and
        plenty of successful output opens by talking about errors — a compiler
        summary, a log excerpt, a grep for the word. Guessing from the text made
        villagers flinch at nothing."""
        self.assertFalse(tool_result_is_error({"content": "Error: no such file"}))
        self.assertFalse(tool_result_is_error(
            {"content": [{"type": "text", "text": "error running command"}]}))
        self.assertTrue(tool_result_is_error(
            {"is_error": True, "content": "Error: no such file"}))

    def test_success_is_not_an_error(self):
        self.assertFalse(tool_result_is_error({"content": "all good"}))
        self.assertFalse(tool_result_is_error({"content": []}))
        self.assertFalse(tool_result_is_error({}))

    def test_word_error_mid_text_is_not_a_failure(self):
        """Avoid false positives — output that merely mentions errors is fine."""
        self.assertFalse(tool_result_is_error({"content": "0 errors, 0 warnings"}))
        self.assertFalse(tool_result_is_error({"content": "Error budget: 12 remaining"}))


class TestAgentMeta(unittest.TestCase):
    def test_describe_real_shape(self):
        meta = {"agentType": "workflow-subagent",
                "description": "verify:mechanism:The held frame",
                "workflowPhase": "Verify", "spawnDepth": 1,
                "requestShape": "foreground", "requestNonInteractive": True}
        got = describe_agent_meta(meta, "a1b2")
        self.assertEqual(got["agent_id"], "a1b2")
        self.assertEqual(got["agent_type"], "workflow-subagent")
        self.assertEqual(got["phase"], "Verify")
        self.assertEqual(got["depth"], 1)
        self.assertTrue(got["foreground"])

    def test_missing_fields_get_defaults(self):
        got = describe_agent_meta({})
        self.assertEqual(got["agent_type"], "unknown")
        self.assertEqual(got["phase"], "")
        self.assertEqual(got["depth"], 0)

    def test_non_dict_is_safe(self):
        self.assertEqual(describe_agent_meta(None)["agent_type"], "unknown")

    def test_agent_id_from_path(self):
        self.assertEqual(agent_id_from_path("/x/subagents/agent-a1b2c3.jsonl"), "a1b2c3")
        self.assertEqual(agent_id_from_path("/x/agent-a1b2c3.meta.json"), "a1b2c3")
        self.assertEqual(agent_id_from_path("/x/other.jsonl"), "other")

    def test_split_agent_label(self):
        self.assertEqual(split_agent_label("verify:the held frame"),
                         ("verify", "the held frame"))
        self.assertEqual(split_agent_label("no colon here"), ("", "no colon here"))
        # A long or spacey prefix is not a stage tag.
        self.assertEqual(split_agent_label("this is a sentence: with a colon"),
                         ("", "this is a sentence: with a colon"))


class TestWorkflowState(unittest.TestCase):
    def test_describe_real_shape(self):
        state = {"runId": "wf_1", "workflowName": "review-changes", "status": "completed",
                 "agentCount": 20, "durationMs": 65000, "startTime": 1789128000000,
                 "summary": "done",
                 "phases": [{"title": "Review", "detail": "look"}, {"title": "Verify"}]}
        got = describe_workflow(state)
        self.assertEqual(got["name"], "review-changes")
        self.assertEqual(got["agent_count"], 20)
        self.assertEqual([p["title"] for p in got["phases"]], ["Review", "Verify"])
        self.assertEqual(got["started_at"], 1789128000.0)

    def test_empty_state_has_safe_defaults(self):
        got = describe_workflow({})
        self.assertEqual(got["name"], "workflow")
        self.assertEqual(got["phases"], [])
        self.assertEqual(got["agent_count"], 0)

    def test_malformed_phases_are_skipped(self):
        got = describe_workflow({"phases": ["nope", {"no_title": 1}, {"title": "Ok"}]})
        self.assertEqual([p["title"] for p in got["phases"]], ["Ok"])


class TestJournal(unittest.TestCase):
    def test_normalizes_lifecycle_rows(self):
        rows = [
            {"type": "launched"},
            {"type": "started", "agentId": "a1", "label": "recon:x", "phase": "Recon"},
            {"type": "result", "agentId": "a1"},
            {"type": "noise", "agentId": "a9"},
        ]
        got = journal_entries(rows)
        self.assertEqual([g["kind"] for g in got], ["launched", "started", "result"])
        self.assertEqual(got[1]["label"], "recon:x")
        self.assertEqual(got[1]["phase"], "Recon")

    def test_junk_rows_are_ignored(self):
        self.assertEqual(journal_entries(["x", None, 5, {}]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
