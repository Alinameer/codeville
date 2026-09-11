"""Tests for project discovery and the lossy slug -> path problem.

Everything here is hermetic: temporary directories stand in for both
``~/.claude/projects`` and the real working directories being pointed at.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from codeville.projects import (  # noqa: E402
    Project,
    discover_projects,
    index_by_path,
    probe_path_from_slug,
    read_cwd_from_transcript,
    resolve_project,
)


def slugify(path: str) -> str:
    """Reproduce Claude Code's lossy flattening, for building fixtures."""
    out = path
    for ch in ("/", ".", "_"):
        out = out.replace(ch, "-")
    return out


class TestSlugifyFixture(unittest.TestCase):
    """Pin the encoding our fixtures assume, taken from real directory names."""

    def test_matches_observed_real_slugs(self):
        cases = [
            ("/home/alinameer/Pictures/taha", "-home-alinameer-Pictures-taha"),
            ("/home/alinameer/Pictures/Beeb-all", "-home-alinameer-Pictures-Beeb-all"),
            ("/home/alinameer/Project/rama.framer.media-1783158233999",
             "-home-alinameer-Project-rama-framer-media-1783158233999"),
            ("/home/alinameer/Documents/Netorase_Phone_-_Iris",
             "-home-alinameer-Documents-Netorase-Phone---Iris"),
        ]
        for path, expected in cases:
            self.assertEqual(slugify(path), expected, path)

    def test_encoding_is_genuinely_ambiguous(self):
        """Different paths collapse to the same slug — hence the cwd-first design."""
        self.assertEqual(slugify("/a/b-c"), slugify("/a/b/c"))
        self.assertEqual(slugify("/a/b.c"), slugify("/a/b_c"))


class ProjectsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="codeville-proj-")
        self.store = os.path.join(self.tmp, "projects")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.store)
        os.makedirs(self.work)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_project(self, real_path, transcripts=1, with_cwd=True, filler=0):
        """Create a fake ~/.claude/projects entry pointing at *real_path*."""
        store_dir = os.path.join(self.store, slugify(real_path))
        os.makedirs(store_dir, exist_ok=True)
        for n in range(transcripts):
            lines = []
            for _ in range(filler):
                lines.append({"type": "queue-operation", "operation": "noop"})
            if with_cwd:
                lines.append({"type": "user", "cwd": real_path, "sessionId": f"s{n}"})
            with open(os.path.join(store_dir, f"sess{n}.jsonl"), "w") as fh:
                for line in lines:
                    fh.write(json.dumps(line) + "\n")
        return store_dir


class TestReadCwd(ProjectsTestCase):
    def test_reads_cwd_from_first_record(self):
        store = self.make_project(self.work)
        transcript = os.path.join(store, "sess0.jsonl")
        self.assertEqual(read_cwd_from_transcript(transcript), self.work)

    def test_finds_cwd_after_filler_records(self):
        store = self.make_project(self.work, filler=50)
        self.assertEqual(read_cwd_from_transcript(os.path.join(store, "sess0.jsonl")),
                         self.work)

    def test_gives_up_past_max_lines(self):
        store = self.make_project(self.work, filler=200)
        path = os.path.join(store, "sess0.jsonl")
        self.assertIsNone(read_cwd_from_transcript(path, max_lines=10))
        self.assertEqual(read_cwd_from_transcript(path, max_lines=500), self.work)

    def test_ignores_malformed_lines(self):
        store = self.make_project(self.work, with_cwd=False)
        path = os.path.join(store, "sess0.jsonl")
        with open(path, "w") as fh:
            fh.write('{"cwd": broken json\n')
            fh.write(json.dumps({"type": "user", "cwd": self.work}) + "\n")
        self.assertEqual(read_cwd_from_transcript(path), self.work)

    def test_rejects_relative_cwd(self):
        store = self.make_project(self.work, with_cwd=False)
        path = os.path.join(store, "sess0.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "user", "cwd": "relative/path"}) + "\n")
        self.assertIsNone(read_cwd_from_transcript(path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(read_cwd_from_transcript(os.path.join(self.tmp, "nope.jsonl")))


class TestResolveProject(ProjectsTestCase):
    def test_cwd_beats_slug_guessing(self):
        """A path with dots and underscores is recovered exactly, though the slug lost them."""
        real = os.path.join(self.work, "rama.framer.media-178")
        os.makedirs(real)
        store = self.make_project(real)
        project = resolve_project(store)
        self.assertEqual(project.path, real)
        self.assertEqual(project.resolved_by, "cwd")
        self.assertTrue(project.exists)

    def test_literal_dash_directory_resolves(self):
        real = os.path.join(self.work, "Beeb-all")
        os.makedirs(real)
        project = resolve_project(self.make_project(real))
        self.assertEqual(project.path, real)

    def test_cwd_used_even_when_directory_was_deleted(self):
        real = os.path.join(self.work, "since-deleted")
        os.makedirs(real)
        store = self.make_project(real)
        shutil.rmtree(real)
        project = resolve_project(store)
        self.assertEqual(project.path, real)
        self.assertFalse(project.exists, "deleted dir must report exists=False")

    def test_stale_project_falls_back_to_probing(self):
        real = os.path.join(self.work, "ghost")
        os.makedirs(real)
        store = os.path.join(self.store, slugify(real))
        os.makedirs(store)  # no transcripts at all
        project = resolve_project(store)
        self.assertTrue(project.is_stale)
        self.assertEqual(project.path, real)
        self.assertEqual(project.resolved_by, "probe")

    def test_unresolvable_stale_project(self):
        store = os.path.join(self.store, "-nonexistent-path-xyzzy-nothing-here")
        os.makedirs(store)
        project = resolve_project(store)
        self.assertIsNone(project.path)
        self.assertEqual(project.resolved_by, "")
        self.assertFalse(project.exists)


class TestProbePathFromSlug(ProjectsTestCase):
    def test_probes_nested_directories(self):
        real = os.path.join(self.work, "outer", "inner")
        os.makedirs(real)
        self.assertEqual(probe_path_from_slug(slugify(real)), real)

    def test_probes_dotted_name(self):
        real = os.path.join(self.work, "site.example.com")
        os.makedirs(real)
        self.assertEqual(probe_path_from_slug(slugify(real)), real)

    def test_probes_underscored_name(self):
        real = os.path.join(self.work, "my_project_v2")
        os.makedirs(real)
        self.assertEqual(probe_path_from_slug(slugify(real)), real)

    def test_returns_none_for_nothing_on_disk(self):
        self.assertIsNone(probe_path_from_slug("-definitely-not-a-real-path-here-xyzzy"))

    def test_search_is_bounded(self):
        """A long ambiguous slug must not hang; the branch cap ends the walk."""
        self.assertIsNone(probe_path_from_slug("-" + "-".join(["seg"] * 40),
                                               max_branches=200))


class TestProjectMetadata(ProjectsTestCase):
    def test_name_prefers_basename_of_real_path(self):
        real = os.path.join(self.work, "my.cool.app")
        os.makedirs(real)
        self.assertEqual(resolve_project(self.make_project(real)).name, "my.cool.app")

    def test_stale_flag_and_session_count(self):
        real = os.path.join(self.work, "busy")
        os.makedirs(real)
        project = resolve_project(self.make_project(real, transcripts=3))
        self.assertFalse(project.is_stale)
        self.assertEqual(len(project.session_files), 3)
        self.assertGreater(project.last_active, 0)

    def test_to_dict_is_json_serializable(self):
        real = os.path.join(self.work, "serial")
        os.makedirs(real)
        payload = resolve_project(self.make_project(real)).to_dict()
        json.dumps(payload)  # must not raise
        self.assertEqual(payload["name"], "serial")
        self.assertEqual(payload["resolved_by"], "cwd")

    def test_last_active_zero_when_no_transcripts(self):
        project = Project(slug="-x", store_dir="/nowhere")
        self.assertEqual(project.last_active, 0)


class TestDiscovery(ProjectsTestCase):
    def test_discovers_and_sorts_by_recency(self):
        import time
        older = os.path.join(self.work, "older")
        newer = os.path.join(self.work, "newer")
        for d in (older, newer):
            os.makedirs(d)
        self.make_project(older)
        time.sleep(0.02)
        self.make_project(newer)

        found = discover_projects(self.store)
        self.assertEqual([p.name for p in found][:2], ["newer", "older"])

    def test_include_stale_toggle(self):
        real = os.path.join(self.work, "live")
        os.makedirs(real)
        self.make_project(real)
        os.makedirs(os.path.join(self.store, "-some-stale-thing"))

        self.assertEqual(len(discover_projects(self.store, include_stale=True)), 2)
        self.assertEqual(len(discover_projects(self.store, include_stale=False)), 1)

    def test_ignores_loose_files_in_projects_dir(self):
        real = os.path.join(self.work, "ok")
        os.makedirs(real)
        self.make_project(real)
        with open(os.path.join(self.store, "stray.txt"), "w") as fh:
            fh.write("not a project")
        self.assertEqual(len(discover_projects(self.store)), 1)

    def test_missing_projects_dir_returns_empty(self):
        self.assertEqual(discover_projects(os.path.join(self.tmp, "absent")), [])

    def test_index_by_path_skips_unresolved(self):
        real = os.path.join(self.work, "indexed")
        os.makedirs(real)
        self.make_project(real)
        os.makedirs(os.path.join(self.store, "-unresolvable-xyzzy-nope"))

        index = index_by_path(discover_projects(self.store))
        self.assertIn(real, index)
        self.assertTrue(all(k is not None for k in index))


if __name__ == "__main__":
    unittest.main(verbosity=2)
