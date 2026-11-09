"""Report: headline reconciliation arithmetic + diagnostics vs truth."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generator import generate_dataset
from helpers import consent_row, dataset, identify, pixel_event, server_event, truth_row
from report import build_report


class TestSeededHeadline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = build_report(generate_dataset(conversions=240, seed=42))
        cls.rec = cls.report["reconciliation"]

    def test_headline_numbers(self):
        self.assertEqual(self.rec["naive_count"], 446)
        self.assertEqual(self.rec["deduped_count"], 240)
        self.assertEqual(self.rec["consent_gated_count"], 213)
        self.assertEqual(self.rec["ground_truth_count"], 240)
        self.assertEqual(self.rec["ground_truth_consented_count"], 213)

    def test_naive_is_raw_sum(self):
        self.assertEqual(self.rec["naive_count"],
                         self.rec["pixel_raw_count"] + self.rec["server_raw_count"])

    def test_gaps_fully_explained(self):
        gaps = self.report["gaps_explained"]
        self.assertEqual(gaps["check_naive_gap"],
                         self.rec["naive_count"] - self.rec["deduped_count"])
        self.assertEqual(gaps["duplicate_deliveries"], 34)
        self.assertEqual(gaps["exact_merges"], 152)
        self.assertEqual(gaps["fallback_merges"], 20)
        self.assertEqual(gaps["dropped_no_consent"],
                         self.rec["deduped_count"] - self.rec["consent_gated_count"])
        self.assertEqual(gaps["dropped_no_consent"], 27)

    def test_reconciliation_error_zero(self):
        self.assertEqual(self.rec["reconciliation_error"], 0)
        self.assertEqual(self.rec["missed_vs_truth"], 0)
        self.assertEqual(self.rec["phantom_vs_truth"], 0)

    def test_rates_measured_vs_truth(self):
        diag = self.report["diagnostics"]
        self.assertEqual(diag["match_rate_vs_truth"], 1.0)
        self.assertEqual(diag["duplicate_rate_vs_truth"], 0.8583)
        self.assertEqual(diag["recovered_vs_truth"], 240)

    def test_channel_diagnostics_shape(self):
        channels = self.report["diagnostics"]["channels"]
        self.assertEqual(len(channels), 8)
        for row in channels.values():
            # Coverage is ONE column beside counts — never the whole story.
            self.assertEqual(set(row), {"ground_truth", "pixel_rows",
                                        "server_rows", "deduped",
                                        "consent_gated", "coverage"})
            self.assertEqual(row["deduped"], row["ground_truth"])


class TestHandBuilt(unittest.TestCase):
    def test_double_count_visible_in_naive_only(self):
        ds = dataset(pixel=[pixel_event()], server=[server_event()],
                     truth=[truth_row()], identifies=[identify()],
                     consent=[consent_row()])
        rec = build_report(ds)["reconciliation"]
        self.assertEqual((rec["naive_count"], rec["deduped_count"],
                          rec["consent_gated_count"]), (2, 1, 1))

    def test_non_consented_never_counted(self):
        ds = dataset(pixel=[pixel_event()], server=[server_event()],
                     truth=[truth_row(consented=False)],
                     identifies=[identify()],
                     consent=[consent_row(consented=False)])
        report = build_report(ds)
        self.assertEqual(report["reconciliation"]["consent_gated_count"], 0)
        self.assertEqual(report["reason_counts"]["DROPPED_NO_CONSENT"], 1)

    def test_report_deterministic(self):
        ds = generate_dataset(conversions=60, seed=42)
        import json
        a = json.dumps(build_report(ds), sort_keys=True)
        b = json.dumps(build_report(generate_dataset(conversions=60, seed=42)),
                       sort_keys=True)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
