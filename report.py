"""Reconciliation report: the headline table + diagnostics.

The headline is ONE table an advertiser-facing engineer can explain
in a minute:

  naive_count          — pixel rows + server rows, summed raw. This
                         is the number that panics people: it counts
                         both-stream conversions twice and counts
                         retried duplicate deliveries again.
  deduped_count        — distinct conversions after reconcile stages
                         1–4 (duplicates dropped, cross-stream pairs
                         merged). Answers "how many distinct
                         conversions were observed?"
  consent_gated_count  — deduped minus non-consented. Answers "how
                         many may we count?"
  ground_truth_count   — how many conversions truly happened, and
  ground_truth_consented — how many of those were consented.

Every gap between neighbouring rows is explained by reason-code
counts, and the arithmetic is exact:

  naive - deduped          = duplicate deliveries + exact merges
                             + fallback merges
  deduped - consent_gated  = dropped-no-consent
  deduped vs ground truth  = missed (truth never observed) and
                             phantom (observed with no truth) —
                             both zero on the seeded dataset

Diagnostics keep per-channel coverage as ONE column among several —
coverage alone flatters a stream (a stream can "cover" a channel by
flooding it with duplicates), so it sits beside counts and rates.
Here ``coverage`` means dual-stream coverage: the share of the
channel's true conversions that BOTH streams observed (taken from
the unified conversions' source lists, so duplicates cannot inflate
it). The rest of the channel's truth arrived via one stream only —
exactly the conversions a single-stream outage would lose.
Match rate and duplicate rate are measured AGAINST GROUND TRUTH
(their denominators and references are truth counts), never
self-reported from the streams' own claims:

  match_rate     = ground-truth conversions recovered exactly once
                   after dedupe / ground-truth conversions
  duplicate_rate = excess raw rows (naive - deduped) /
                   ground-truth conversions
"""

from __future__ import annotations

from typing import Dict, List

from generator import CHANNELS
from reconcile import (REASON_DUPLICATE, REASON_EXACT, REASON_FALLBACK,
                       REASON_NO_CONSENT, apply_consent_gate, reconcile)


def _recovered_once(unified: List[Dict],
                    ground_truth: List[Dict]) -> int:
    """Ground-truth conversions whose event_id appears exactly once
    among unified conversions (fallback merges carry the pixel
    event_id, so they count as recovered)."""
    counts: Dict[str, int] = {}
    for conv in unified:
        if conv.get("event_id"):
            counts[conv["event_id"]] = counts.get(conv["event_id"], 0) + 1
    return sum(1 for row in ground_truth if counts.get(row["event_id"], 0) == 1)


def build_report(dataset: Dict[str, List[Dict]]) -> Dict:
    pixel = dataset["pixel"]
    server = dataset["server"]
    truth = dataset["ground_truth"]
    result = reconcile(pixel, server, dataset["identifies"])
    unified = result["unified"]
    gate = apply_consent_gate(unified, dataset["consent"])
    counted, dropped = gate["counted"], gate["dropped"]
    reasons = dict(result["reason_counts"])
    reasons[REASON_NO_CONSENT] = len(dropped)

    naive = len(pixel) + len(server)
    deduped = len(unified)
    gated = len(counted)
    truth_total = len(truth)
    truth_consented = sum(1 for r in truth if r["consented"] is True)
    recovered = _recovered_once(unified, truth)
    truth_ids = {r["event_id"] for r in truth}
    phantom = sum(1 for c in unified
                  if c.get("event_id") and c["event_id"] not in truth_ids)
    missed = truth_total - recovered

    reconciliation = {
        "naive_count": naive,
        "deduped_count": deduped,
        "consent_gated_count": gated,
        "ground_truth_count": truth_total,
        "ground_truth_consented_count": truth_consented,
        "pixel_raw_count": len(pixel),
        "server_raw_count": len(server),
        "gap_naive_to_deduped": naive - deduped,
        "gap_deduped_to_gated": deduped - gated,
        "reconciliation_error": gated - truth_consented,
        "missed_vs_truth": missed,
        "phantom_vs_truth": phantom,
    }
    gaps_explained = {
        "duplicate_deliveries": reasons[REASON_DUPLICATE],
        "exact_merges": reasons[REASON_EXACT] // 2,
        "fallback_merges": reasons[REASON_FALLBACK] // 2,
        "dropped_no_consent": reasons[REASON_NO_CONSENT],
        "check_naive_gap": (reasons[REASON_DUPLICATE]
                            + reasons[REASON_EXACT] // 2
                            + reasons[REASON_FALLBACK] // 2),
    }

    match_rate = round(recovered / truth_total, 4) if truth_total else 0.0
    duplicate_rate = round((naive - deduped) / truth_total, 4) if truth_total else 0.0

    channels: Dict[str, Dict] = {}
    for channel in CHANNELS:
        t_rows = [r for r in truth if r["channel"] == channel]
        p_rows = [e for e in pixel if e["channel"] == channel]
        s_rows = [e for e in server if e["channel"] == channel]
        d_rows = [c for c in unified if c["channel"] == channel]
        g_rows = [c for c in counted if c["channel"] == channel]
        dual = sum(1 for c in d_rows if c["sources"] == ["pixel", "server"])
        coverage = round(dual / len(t_rows), 4) if t_rows else 0.0
        channels[channel] = {
            "ground_truth": len(t_rows),
            "pixel_rows": len(p_rows),
            "server_rows": len(s_rows),
            "deduped": len(d_rows),
            "consent_gated": len(g_rows),
            "coverage": coverage,
        }

    return {
        "reconciliation": reconciliation,
        "reason_counts": reasons,
        "gaps_explained": gaps_explained,
        "diagnostics": {
            "match_rate_vs_truth": match_rate,
            "duplicate_rate_vs_truth": duplicate_rate,
            "recovered_vs_truth": recovered,
            "channels": channels,
        },
        "unified_count": deduped,
        "counted": counted,
    }
