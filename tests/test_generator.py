"""Generator: determinism, shape, planted defects, IO."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generator import (CHANNELS, generate_dataset, load_dataset, load_events,
                       parse_timestamp, tables_to_jsonl, write_dataset)


class TestGenerator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = generate_dataset(conversions=240, seed=42)

    def test_same_seed_identical(self):
        self.assertEqual(generate_dataset(conversions=50, seed=7),
                         generate_dataset(conversions=50, seed=7))

    def test_different_seed_differs(self):
        self.assertNotEqual(generate_dataset(conversions=50, seed=7),
                            generate_dataset(conversions=50, seed=8))

    def test_jsonl_bytes_deterministic(self):
        a = tables_to_jsonl(generate_dataset(conversions=50, seed=7), "pixel")
        b = tables_to_jsonl(generate_dataset(conversions=50, seed=7), "pixel")
        self.assertEqual(a, b)

    def test_zero_conversions(self):
        ds = generate_dataset(conversions=0, seed=42)
        for table in ("ground_truth", "pixel", "server", "identifies", "consent"):
            self.assertEqual(ds[table], [])

    def test_negative_conversions_raises(self):
        with self.assertRaises(ValueError):
            generate_dataset(conversions=-1)

    def test_truth_shape_and_vocab(self):
        self.assertEqual(len(self.ds["ground_truth"]), 240)
        for row in self.ds["ground_truth"]:
            self.assertIn(row["channel"], CHANNELS)
            self.assertGreater(row["revenue"], 0)
            self.assertIsInstance(row["consented"], bool)
            self.assertTrue(row["event_id"].startswith("EVT-"))
        ids = [r["conversion_id"] for r in self.ds["ground_truth"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_truth_observed_at_least_once(self):
        # Every true conversion is seen by >= 1 stream: its event_id is
        # on a pixel row, or on a server row, or it is a fallback case
        # (pixel carries the id, server carries none).
        pixel_ids = {e["event_id"] for e in self.ds["pixel"]}
        server_ids = {e["event_id"] for e in self.ds["server"]}
        for row in self.ds["ground_truth"]:
            self.assertIn(row["event_id"], pixel_ids | server_ids)

    def test_both_pixel_only_server_only_all_present(self):
        pixel_ids = {e["event_id"] for e in self.ds["pixel"]}
        server_ids = {e["event_id"] for e in self.ds["server"]
                      if e["event_id"] is not None}
        truth_ids = {r["event_id"] for r in self.ds["ground_truth"]}
        both = truth_ids & pixel_ids & server_ids
        pixel_only = (truth_ids & pixel_ids) - server_ids
        server_only = (truth_ids & server_ids) - pixel_ids
        # Fallback rows hide some both-stream ids from the server-id
        # set, so pixel_only here is an upper bound; all three patterns
        # must be non-empty even under that accounting.
        self.assertTrue(both)
        self.assertTrue(server_only)
        self.assertGreater(len(pixel_only), 0)

    def test_duplicate_deliveries_planted(self):
        for stream in ("pixel", "server"):
            ids = [e["event_id"] for e in self.ds[stream]
                   if e["event_id"] is not None]
            self.assertLess(len(set(ids)), len(ids), stream)

    def test_fallback_rows_planted(self):
        noid = [e for e in self.ds["server"] if e["event_id"] is None]
        self.assertTrue(noid)

    def test_timestamp_skew_server_behind_pixel(self):
        pixel_by_id = {}
        for e in self.ds["pixel"]:
            pixel_by_id.setdefault(e["event_id"], e)
        checked = 0
        for e in self.ds["server"]:
            if e["event_id"] in pixel_by_id:
                gap = (parse_timestamp(e["timestamp"])
                       - parse_timestamp(pixel_by_id[e["event_id"]]["timestamp"])
                       ).total_seconds()
                self.assertGreater(gap, 0)
                checked += 1
        self.assertGreater(checked, 50)

    def test_non_consented_events_present_in_raw_streams(self):
        nonconsented_people = {r["anon_id"] for r in self.ds["consent"]
                               if r["consented"] is False}
        self.assertTrue(nonconsented_people)
        raw_people = ({e["anon_id"] for e in self.ds["pixel"]}
                      | {e["user_id"] for e in self.ds["server"]})
        truth_nonconsented = {r["event_id"] for r in self.ds["ground_truth"]
                              if r["consented"] is False}
        self.assertTrue(truth_nonconsented)
        pixel_ids = {e["event_id"] for e in self.ds["pixel"]}
        server_ids = {e["event_id"] for e in self.ds["server"]}
        self.assertTrue(truth_nonconsented & (pixel_ids | server_ids))

    def test_consent_covers_every_person_and_identifies_link(self):
        people = {r["anon_id"] for r in self.ds["ground_truth"]}
        self.assertTrue(people <= {r["anon_id"] for r in self.ds["consent"]})
        consent_by_anon = {r["anon_id"]: r for r in self.ds["consent"]}
        for row in self.ds["identifies"]:
            self.assertEqual(consent_by_anon[row["anon_id"]]["user_id"],
                             row["user_id"])

    def test_fallback_only_on_identified_people(self):
        # An id-less server row must belong to an identified person,
        # else fallback matching would be impossible by construction.
        identified_users = {r["user_id"] for r in self.ds["identifies"]}
        for e in self.ds["server"]:
            if e["event_id"] is None:
                self.assertIn(e["user_id"], identified_users)

    def test_streams_sorted_by_timestamp(self):
        for stream in ("pixel", "server"):
            stamps = [e["timestamp"] for e in self.ds[stream]]
            self.assertEqual(stamps, sorted(stamps))


class TestSerializersAndLoaders(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="am-gen-test-")
        self.ds = generate_dataset(conversions=40, seed=11)

    def test_jsonl_dataset_round_trip(self):
        write_dataset(self.ds, self.tmp, fmt="jsonl")
        self.assertEqual(load_dataset(self.tmp), self.ds)

    def test_csv_round_trip_with_null_coercion(self):
        write_dataset(self.ds, self.tmp, fmt="csv")
        loaded = load_dataset(self.tmp)
        self.assertEqual(loaded, self.ds)
        for row in loaded["server"]:
            self.assertTrue(row["event_id"] is None
                            or isinstance(row["event_id"], str))
        for row in loaded["ground_truth"]:
            self.assertIsInstance(row["revenue"], float)
            self.assertIsInstance(row["consented"], bool)

    def test_load_individual_files(self):
        write_dataset(self.ds, self.tmp, fmt="jsonl")
        self.assertEqual(load_events(os.path.join(self.tmp, "pixel.jsonl")),
                         self.ds["pixel"])

    def test_missing_dataset_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_dataset("/tmp/no-such-dataset-dir-xyz")


if __name__ == "__main__":
    unittest.main()
