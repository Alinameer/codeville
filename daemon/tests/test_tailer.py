"""Tests for the JSONL tailer, including the nasty real-world cases:
partial lines, truncation, file replacement, oversized records and huge files.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from codeville.tailer import JsonlTailer, read_last_records  # noqa: E402


class TailerTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="codeville-tail-")
        self.path = os.path.join(self.dir, "session.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, *records, mode="a"):
        with open(self.path, mode) as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def write_raw(self, text, mode="a"):
        with open(self.path, mode) as fh:
            fh.write(text)


class TestBasicTailing(TailerTestCase):
    def test_track_from_start_reads_everything(self):
        self.write({"i": 1}, {"i": 2})
        t = JsonlTailer()
        t.track(self.path, start_at_end=False)
        self.assertEqual([r["i"] for r in t.poll(self.path).records], [1, 2])

    def test_only_new_records_on_second_poll(self):
        self.write({"i": 1})
        t = JsonlTailer()
        t.track(self.path, start_at_end=False)
        t.poll(self.path)
        self.write({"i": 2}, {"i": 3})
        self.assertEqual([r["i"] for r in t.poll(self.path).records], [2, 3])
        self.assertEqual(t.poll(self.path).records, [])

    def test_start_at_end_skips_history(self):
        self.write(*[{"i": i} for i in range(100)])
        t = JsonlTailer()
        t.track(self.path, start_at_end=True, prime_bytes=0)
        self.assertEqual(t.poll(self.path).records, [])
        self.write({"i": "fresh"})
        self.assertEqual([r["i"] for r in t.poll(self.path).records], ["fresh"])

    def test_priming_window_surfaces_recent_lines(self):
        self.write(*[{"i": i} for i in range(500)])
        t = JsonlTailer()
        # A window far smaller than the file: we should get a recent suffix only.
        t.track(self.path, start_at_end=True, prime_bytes=200)
        got = [r["i"] for r in t.poll(self.path).records]
        self.assertTrue(got, "priming window should surface something")
        self.assertEqual(got[-1], 499)
        self.assertLess(len(got), 500, "should not have replayed the whole file")

    def test_tracking_is_idempotent(self):
        self.write({"i": 1})
        t = JsonlTailer()
        self.assertTrue(t.track(self.path, start_at_end=False))
        self.assertFalse(t.track(self.path, start_at_end=False))

    def test_missing_file_is_not_tracked(self):
        t = JsonlTailer()
        self.assertFalse(t.track(os.path.join(self.dir, "nope.jsonl")))
        self.assertEqual(t.poll(os.path.join(self.dir, "nope.jsonl")).records, [])

    def test_forget_stops_tracking(self):
        self.write({"i": 1})
        t = JsonlTailer()
        t.track(self.path, start_at_end=False)
        t.forget(self.path)
        self.assertEqual(t.tracked, ())
        self.assertEqual(t.poll(self.path).records, [])


class TestPartialWrites(TailerTestCase):
    def test_incomplete_line_is_withheld_then_completed(self):
        t = JsonlTailer()
        self.write_raw('{"i": 1}\n{"i": 2', mode="w")
        t.track(self.path, start_at_end=False)
        first = t.poll(self.path)
        self.assertEqual([r["i"] for r in first.records], [1])
        self.assertEqual(first.malformed, 0, "a partial line is not malformed")

        self.write_raw('}\n')
        self.assertEqual([r["i"] for r in t.poll(self.path).records], [2])

    def test_record_split_across_three_reads(self):
        t = JsonlTailer()
        self.write_raw("", mode="w")
        t.track(self.path, start_at_end=False)
        for piece in ('{"na', 'me": "ow', 'l"}\n'):
            self.write_raw(piece)
            res = t.poll(self.path)
        self.assertEqual([r["name"] for r in res.records], ["owl"])

    def test_blank_lines_ignored(self):
        self.write_raw('{"i":1}\n\n\n{"i":2}\n', mode="w")
        t = JsonlTailer()
        t.track(self.path, start_at_end=False)
        res = t.poll(self.path)
        self.assertEqual([r["i"] for r in res.records], [1, 2])
        self.assertEqual(res.malformed, 0)

    def test_malformed_json_is_counted_not_raised(self):
        self.write_raw('{"ok":1}\nnot json at all\n{"ok":2}\n', mode="w")
        t = JsonlTailer()
        t.track(self.path, start_at_end=False)
        res = t.poll(self.path)
        self.assertEqual([r["ok"] for r in res.records], [1, 2])
        self.assertEqual(res.malformed, 1)


class TestRotationAndTruncation(TailerTestCase):
    def test_truncation_resets_cursor(self):
        self.write(*[{"i": i} for i in range(20)])
        t = JsonlTailer()
        t.track(self.path, start_at_end=False)
        t.poll(self.path)

        self.write({"i": "after"}, mode="w")  # truncate + rewrite
        res = t.poll(self.path)
        self.assertTrue(res.reset)
        self.assertEqual([r["i"] for r in res.records], ["after"])

    def test_replaced_file_new_inode_resets_cursor(self):
        self.write({"i": 1})
        t = JsonlTailer()
        t.track(self.path, start_at_end=False)
        t.poll(self.path)

        replacement = os.path.join(self.dir, "tmp.jsonl")
        with open(replacement, "w") as fh:
            fh.write(json.dumps({"i": "replaced"}) + "\n")
        os.replace(replacement, self.path)  # different inode

        res = t.poll(self.path)
        self.assertTrue(res.reset)
        self.assertEqual([r["i"] for r in res.records], ["replaced"])


class TestOversizedLines(TailerTestCase):
    def test_oversized_line_skipped_but_stream_continues(self):
        t = JsonlTailer(max_line_bytes=512)
        self.write_raw("", mode="w")
        t.track(self.path, start_at_end=False)
        self.write({"small": 1}, {"huge": "x" * 5000}, {"small": 2})
        res = t.poll(self.path)
        self.assertEqual(res.oversized, 1)
        self.assertEqual([r.get("small") for r in res.records], [1, 2])

    def test_runaway_unterminated_line_is_abandoned(self):
        t = JsonlTailer(max_line_bytes=256)
        self.write_raw("", mode="w")
        t.track(self.path, start_at_end=False)
        self.write_raw('{"x":"' + "y" * 4000)  # no newline, ever
        res = t.poll(self.path)
        self.assertEqual(res.oversized, 1)
        # The buffer must not keep growing across polls.
        self.write_raw("z" * 4000)
        self.assertEqual(t.poll(self.path).oversized, 1)


class TestStatePersistence(TailerTestCase):
    def test_state_roundtrip_resumes_where_it_left_off(self):
        self.write({"i": 1}, {"i": 2})
        t1 = JsonlTailer()
        t1.track(self.path, start_at_end=False)
        t1.poll(self.path)
        state = t1.save_state()

        self.write({"i": 3})
        t2 = JsonlTailer()
        t2.load_state(state)
        self.assertEqual([r["i"] for r in t2.poll(self.path).records], [3])

    def test_state_for_shrunken_file_restarts(self):
        self.write(*[{"i": i} for i in range(50)])
        t1 = JsonlTailer()
        t1.track(self.path, start_at_end=False)
        t1.poll(self.path)
        state = t1.save_state()

        self.write({"i": "tiny"}, mode="w")
        t2 = JsonlTailer()
        t2.load_state(state)
        self.assertEqual([r["i"] for r in t2.poll(self.path).records], ["tiny"])

    def test_state_for_deleted_file_is_dropped(self):
        self.write({"i": 1})
        t1 = JsonlTailer()
        t1.track(self.path, start_at_end=False)
        state = t1.save_state()
        os.remove(self.path)
        t2 = JsonlTailer()
        t2.load_state(state)
        self.assertEqual(t2.tracked, ())


class TestPollAll(TailerTestCase):
    def test_poll_all_yields_only_files_with_news(self):
        other = os.path.join(self.dir, "other.jsonl")
        with open(other, "w") as fh:
            fh.write(json.dumps({"i": "o"}) + "\n")
        self.write({"i": "s"})

        t = JsonlTailer()
        t.track(self.path, start_at_end=False)
        t.track(other, start_at_end=False)
        self.assertEqual(len(list(t.poll_all())), 2)
        self.assertEqual(list(t.poll_all()), [])

        self.write({"i": "s2"})
        results = list(t.poll_all())
        self.assertEqual([r.path for r in results], [self.path])


class TestReadLastRecords(TailerTestCase):
    def test_returns_trailing_records(self):
        self.write(*[{"i": i} for i in range(1000)])
        got = read_last_records(self.path, limit=5)
        self.assertEqual([r["i"] for r in got], [995, 996, 997, 998, 999])

    def test_small_file_returns_all(self):
        self.write({"i": 1}, {"i": 2})
        self.assertEqual([r["i"] for r in read_last_records(self.path, limit=10)], [1, 2])

    def test_widens_window_when_records_are_large(self):
        # Each record ~2KB; a 4KB window alone could not hold 10 of them.
        self.write(*[{"i": i, "pad": "p" * 2000} for i in range(100)])
        got = read_last_records(self.path, limit=10, window=4096)
        self.assertEqual(len(got), 10)
        self.assertEqual(got[-1]["i"], 99)

    def test_missing_file_returns_empty(self):
        self.assertEqual(read_last_records(os.path.join(self.dir, "gone.jsonl")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
