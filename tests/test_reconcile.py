"""Reconcile: dedupe stages, fallback rules, identity, consent gate."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import consent_row, identify, pixel_event, server_event
from reconcile import (FALLBACK_WINDOW_SECONDS, apply_consent_gate,
                       build_identity_map, identity_key, reconcile,
                       resolve_user_id)


class TestExactMerge(unittest.TestCase):
    def test_exact_id_merges_pair(self):
        res = reconcile([pixel_event()], [server_event()], [identify()])
        self.assertEqual(len(res["unified"]), 1)
        conv = res["unified"][0]
        self.assertEqual(conv["sources"], ["pixel", "server"])
        self.assertEqual(conv["match_reason"], "MERGED_EXACT_ID")
        self.assertEqual(res["reason_counts"]["MERGED_EXACT_ID"], 2)

    def test_exact_merge_needs_no_identity(self):
        # No identify event at all: the shared id still links the pair.
        res = reconcile([pixel_event()], [server_event()], [])
        self.assertEqual(len(res["unified"]), 1)
        self.assertEqual(res["unified"][0]["match_reason"], "MERGED_EXACT_ID")

    def test_exact_merge_ignores_skew(self):
        late = server_event(timestamp="2026-01-10T14:00:00Z")  # 2h behind
        res = reconcile([pixel_event()], [late], [])
        self.assertEqual(len(res["unified"]), 1)

    def test_pixel_timestamp_wins(self):
        res = reconcile([pixel_event()], [server_event()], [])
        self.assertEqual(res["unified"][0]["timestamp"], "2026-01-10T12:00:00Z")


class TestDuplicates(unittest.TestCase):
    def test_duplicate_delivery_dropped(self):
        res = reconcile([pixel_event(), pixel_event()], [], [])
        self.assertEqual(len(res["unified"]), 1)
        self.assertEqual(res["reason_counts"]["DUPLICATE_DELIVERY"], 1)
        dropped = [a for a in res["audit"]
                   if a["reason_code"] == "DUPLICATE_DELIVERY"]
        self.assertEqual(dropped[0]["disposition"], "dropped")

    def test_idless_duplicate_row_dropped(self):
        noid = server_event(event_id=None)
        res = reconcile([], [noid, dict(noid)], [])
        self.assertEqual(len(res["unified"]), 1)
        self.assertEqual(res["reason_counts"]["DUPLICATE_DELIVERY"], 1)

    def test_distinct_events_same_id_in_different_streams_not_duplicates(self):
        res = reconcile([pixel_event()], [server_event()], [])
        self.assertEqual(res["reason_counts"]["DUPLICATE_DELIVERY"], 0)


class TestFallback(unittest.TestCase):
    def setUp(self):
        self.pixel = pixel_event(user_id="HH-U00001")
        self.noid_server = server_event(event_id=None)

    def test_fallback_merges_within_window(self):
        res = reconcile([self.pixel], [self.noid_server], [identify()])
        self.assertEqual(len(res["unified"]), 1)
        self.assertEqual(res["unified"][0]["match_reason"],
                         "MERGED_FALLBACK_WINDOW")

    def test_fallback_via_identify_link_only(self):
        # Pixel knows only the anon id; the identify event is the bridge.
        anon_pixel = pixel_event(user_id=None)
        res = reconcile([anon_pixel], [self.noid_server], [identify()])
        self.assertEqual(len(res["unified"]), 1)
        self.assertEqual(res["unified"][0]["match_reason"],
                         "MERGED_FALLBACK_WINDOW")

    def test_no_identify_no_fallback(self):
        # Deterministic-only stitching: without an explicit identify
        # event the anon pixel and the user server event must NOT be
        # guessed into one conversion.
        anon_pixel = pixel_event(user_id=None)
        res = reconcile([anon_pixel], [self.noid_server], [])
        self.assertEqual(len(res["unified"]), 2)
        reasons = {c["match_reason"] for c in res["unified"]}
        self.assertEqual(reasons, {"KEPT_PIXEL_ONLY", "KEPT_SERVER_ONLY"})

    def test_fallback_outside_window_no_merge(self):
        late = server_event(event_id=None, timestamp="2026-01-10T12:30:00Z")
        gap = 30 * 60
        self.assertGreater(gap, FALLBACK_WINDOW_SECONDS)
        res = reconcile([self.pixel], [late], [identify()])
        self.assertEqual(len(res["unified"]), 2)

    def test_fallback_at_window_edge_merges(self):
        edge = server_event(event_id=None, timestamp="2026-01-10T12:15:00Z")
        res = reconcile([self.pixel], [edge], [identify()])
        self.assertEqual(len(res["unified"]), 1)

    def test_fallback_revenue_mismatch_no_merge(self):
        other = server_event(event_id=None, revenue=249.0)
        res = reconcile([self.pixel], [other], [identify()])
        self.assertEqual(len(res["unified"]), 2)

    def test_fallback_channel_mismatch_no_merge(self):
        other = server_event(event_id=None, channel="email")
        res = reconcile([self.pixel], [other], [identify()])
        self.assertEqual(len(res["unified"]), 2)

    def test_fallback_one_to_one(self):
        # Two id-less server events, one pixel: only the nearer merges.
        near = server_event(event_id=None, timestamp="2026-01-10T12:03:00Z")
        far = server_event(event_id=None, timestamp="2026-01-10T12:10:00Z",
                           revenue=100.0)
        res = reconcile([self.pixel], [near, far], [identify()])
        self.assertEqual(len(res["unified"]), 2)
        self.assertEqual(res["reason_counts"]["MERGED_FALLBACK_WINDOW"], 2)


class TestSingletonsAndAudit(unittest.TestCase):
    def test_pixel_only_and_server_only_kept(self):
        res = reconcile([pixel_event(event_id="EVT-A")],
                        [server_event(event_id="EVT-B")], [])
        self.assertEqual(len(res["unified"]), 2)
        self.assertEqual(res["reason_counts"]["KEPT_PIXEL_ONLY"], 1)
        self.assertEqual(res["reason_counts"]["KEPT_SERVER_ONLY"], 1)

    def test_every_raw_event_audited_once_with_reason(self):
        pixel = [pixel_event(), pixel_event(),
                 pixel_event(event_id="EVT-X")]
        server = [server_event(), server_event(event_id=None,
                                               user_id="HH-U00009")]
        res = reconcile(pixel, server, [identify()])
        self.assertEqual(len(res["audit"]), len(pixel) + len(server))
        for record in res["audit"]:
            self.assertIn(record["disposition"], ("merged", "kept", "dropped"))
            self.assertTrue(record["reason_code"])
        self.assertEqual(sum(res["reason_counts"].values()), len(res["audit"]))


class TestIdentity(unittest.TestCase):
    def test_identity_map_from_identifies_only(self):
        self.assertEqual(build_identity_map([identify()]),
                         {"ANON-0001": "HH-U00001"})
        self.assertEqual(build_identity_map([]), {})

    def test_resolve_prefers_explicit_user_then_identify(self):
        m = build_identity_map([identify()])
        self.assertEqual(resolve_user_id(pixel_event(user_id="HH-U00007"), m),
                         "HH-U00007")
        self.assertEqual(resolve_user_id(pixel_event(user_id=None), m),
                         "HH-U00001")
        self.assertIsNone(resolve_user_id(
            pixel_event(anon_id="ANON-9999", user_id=None), m))

    def test_identity_key_falls_back_to_anon(self):
        self.assertEqual(identity_key(pixel_event(user_id=None), {}),
                         "anon:ANON-0001")


class TestConsentGate(unittest.TestCase):
    def _unified(self):
        res = reconcile([pixel_event()], [server_event()], [identify()])
        return res["unified"]

    def test_consented_counted(self):
        gate = apply_consent_gate(self._unified(), [consent_row(consented=True)])
        self.assertEqual(len(gate["counted"]), 1)
        self.assertEqual(gate["dropped"], [])

    def test_non_consented_dropped_and_counted(self):
        gate = apply_consent_gate(self._unified(),
                                  [consent_row(consented=False)])
        self.assertEqual(gate["counted"], [])
        self.assertEqual(len(gate["dropped"]), 1)
        self.assertEqual(gate["dropped"][0]["reason_code"], "DROPPED_NO_CONSENT")

    def test_missing_consent_record_fails_closed(self):
        gate = apply_consent_gate(self._unified(), [])
        self.assertEqual(gate["counted"], [])
        self.assertEqual(len(gate["dropped"]), 1)


if __name__ == "__main__":
    unittest.main()
