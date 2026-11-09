"""Validation: clean data passes, planted defects flagged, never raises."""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generator import generate_dataset
from helpers import consent_row, dataset, identify, pixel_event, truth_row
from validation import validate_dataset, validate_event, validate_ground_truth


def clean_dataset():
    return dataset(pixel=[pixel_event()], server=[],
                   truth=[truth_row()], identifies=[identify()],
                   consent=[consent_row()])


class TestClean(unittest.TestCase):
    def test_handbuilt_clean(self):
        self.assertEqual(validate_dataset(clean_dataset()), [])

    def test_generated_clean(self):
        self.assertEqual(validate_dataset(generate_dataset(conversions=200,
                                                           seed=42)), [])

    def test_empty_dataset_flagged(self):
        issues = validate_dataset(dataset())
        self.assertEqual(len(issues), 1)

    def test_null_event_id_is_legal(self):
        self.assertEqual(validate_event(
            {**pixel_event(event_id=None), "source": "server",
             "user_id": "HH-U00001", "anon_id": None}, "server"), [])


class TestEvents(unittest.TestCase):
    def test_missing_field(self):
        bad = pixel_event()
        del bad["channel"]
        issues = validate_event(bad, "pixel")
        self.assertTrue(any("missing field 'channel'" in i for i in issues))

    def test_missing_everything_reports_not_raises(self):
        issues = validate_event({}, "pixel")
        self.assertEqual(len(issues), 7)

    def test_invalid_channel(self):
        issues = validate_event(pixel_event(channel="bogus_ads"), "pixel")
        self.assertTrue(any("invalid channel" in i for i in issues))

    def test_source_stream_mismatch(self):
        issues = validate_event(pixel_event(), "server")
        self.assertTrue(any("does not match stream" in i for i in issues))

    def test_negative_revenue(self):
        issues = validate_event(pixel_event(revenue=-1.0), "pixel")
        self.assertTrue(any("revenue must be >= 0" in i for i in issues))

    def test_unparseable_timestamp(self):
        issues = validate_event(pixel_event(timestamp="soon-ish"), "pixel")
        self.assertTrue(any("unparseable timestamp" in i for i in issues))

    def test_no_identity_flagged(self):
        issues = validate_event(pixel_event(anon_id=None, user_id=None), "pixel")
        self.assertTrue(any("no identity" in i for i in issues))

    def test_empty_string_event_id_flagged(self):
        issues = validate_event(pixel_event(event_id=""), "pixel")
        self.assertTrue(any("null or non-empty" in i for i in issues))


class TestTruthAndConsent(unittest.TestCase):
    def test_duplicate_conversion_id(self):
        ds = clean_dataset()
        ds["ground_truth"].append(copy.deepcopy(ds["ground_truth"][0]))
        issues = validate_dataset(ds)
        self.assertTrue(any("duplicate conversion_id" in i for i in issues))

    def test_duplicate_truth_event_id(self):
        ds = clean_dataset()
        dupe = truth_row(conversion_id="CNV-T0002")
        ds["ground_truth"].append(dupe)
        issues = validate_dataset(ds)
        self.assertTrue(any("duplicate ground-truth event_id" in i for i in issues))

    def test_non_bool_consented_flagged(self):
        issues = validate_ground_truth(truth_row(consented="yes"))
        self.assertTrue(any("must be a boolean" in i for i in issues))

    def test_duplicate_consent_row(self):
        ds = clean_dataset()
        ds["consent"].append(dict(ds["consent"][0]))
        issues = validate_dataset(ds)
        self.assertTrue(any("duplicate consent row" in i for i in issues))

    def test_identify_linking_nothing_flagged(self):
        ds = clean_dataset()
        ds["identifies"] = [{"anon_id": "ANON-0001", "user_id": "",
                             "timestamp": "2026-01-10T11:00:00Z"}]
        issues = validate_dataset(ds)
        self.assertTrue(any("must link" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
