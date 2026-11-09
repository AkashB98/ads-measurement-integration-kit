"""Golden evals for ads-measurement-integration-kit.

SYNTHETIC DATA ONLY (fictional advertiser Helios Home). Every eval is
deterministic: run_evals.py writes eval_report.json and the file must
be byte-identical across runs. No timestamps, ports, or temp paths
enter the report — only names, booleans, pinned counts and hashes.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import threading
import urllib.request
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import EXIT_ERROR, EXIT_FAIL, EXIT_OK, main as cli_main, make_server, reset_state  # noqa: E402
from generator import (generate_dataset, load_consent, load_dataset,  # noqa: E402
                       load_events, load_ground_truth, load_identifies,
                       tables_to_jsonl, write_dataset)
from reconcile import apply_consent_gate, reconcile  # noqa: E402
from report import build_report  # noqa: E402
from validation import validate_dataset  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(ROOT, "fixtures")
RESULTS = []

TRUTH_MD5 = "b84ecf71b51279eb52d7df4c885e7aed"


def check(name, passed, detail=""):
    RESULTS.append({"name": name, "passed": bool(passed), "detail": detail})
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return passed


def _run_cli(argv):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return cli_main(argv)


def _pixel(eid="EVT-E1", anon="ANON-E1", user=None, ts="2026-01-10T12:00:00Z",
           channel="paid_search", revenue=100.0):
    return {"event_id": eid, "source": "pixel", "anon_id": anon,
            "user_id": user, "timestamp": ts, "channel": channel,
            "revenue": revenue}


def _server(eid="EVT-E1", user="HH-U-E1", ts="2026-01-10T12:05:00Z",
            channel="paid_search", revenue=100.0):
    return {"event_id": eid, "source": "server", "anon_id": None,
            "user_id": user, "timestamp": ts, "channel": channel,
            "revenue": revenue}


def main():
    print("== golden evals: ads-measurement-integration-kit ==")
    ds = generate_dataset(conversions=240, seed=42)
    report = build_report(ds)
    rec = report["reconciliation"]

    # E1: generator determinism (ground-truth bytes pinned)
    text1 = tables_to_jsonl(generate_dataset(conversions=240, seed=42),
                            "ground_truth")
    text2 = tables_to_jsonl(generate_dataset(conversions=240, seed=42),
                            "ground_truth")
    md5 = hashlib.md5(text1.encode()).hexdigest()
    check("E1 generator determinism (seed=42, 240 conversions)",
          text1 == text2 and md5 == TRUTH_MD5, f"ground_truth_jsonl_md5={md5}")

    # E2: headline reconciliation pinned
    revenue = round(sum(r["revenue"] for r in ds["ground_truth"]), 2)
    check("E2 headline pinned (naive 446, deduped 240, gated 213, truth 240/213)",
          rec["naive_count"] == 446 and rec["deduped_count"] == 240
          and rec["consent_gated_count"] == 213
          and rec["ground_truth_count"] == 240
          and rec["ground_truth_consented_count"] == 213
          and revenue == 50325.0,
          f"naive={rec['naive_count']} deduped={rec['deduped_count']} "
          f"gated={rec['consent_gated_count']} revenue={revenue}")

    # E3: every gap explained by reason counts, arithmetic exact
    gaps = report["gaps_explained"]
    check("E3 gaps explained (206 = 34 dup + 152 exact + 20 fallback; 27 no-consent)",
          gaps["check_naive_gap"] == 206
          and gaps["check_naive_gap"] == rec["naive_count"] - rec["deduped_count"]
          and gaps["duplicate_deliveries"] == 34
          and gaps["exact_merges"] == 152 and gaps["fallback_merges"] == 20
          and gaps["dropped_no_consent"] == 27
          and rec["deduped_count"] - rec["consent_gated_count"] == 27
          and rec["reconciliation_error"] == 0
          and rec["missed_vs_truth"] == 0 and rec["phantom_vs_truth"] == 0)

    # E4: per-event reason counts pinned
    rc = report["reason_counts"]
    check("E4 reason counts pinned (audit tallies per raw event)",
          rc["DUPLICATE_DELIVERY"] == 34 and rc["MERGED_EXACT_ID"] == 304
          and rc["MERGED_FALLBACK_WINDOW"] == 40
          and rc["KEPT_PIXEL_ONLY"] == 33 and rc["KEPT_SERVER_ONLY"] == 35
          and rc["DROPPED_NO_CONSENT"] == 27,
          f"reasons={rc}")

    # E5: rates vs ground truth + dual-stream coverage pins
    diag = report["diagnostics"]
    ch = diag["channels"]
    check("E5 match 1.0 / duplicate 0.8583 vs truth; coverage pinned",
          diag["match_rate_vs_truth"] == 1.0
          and diag["duplicate_rate_vs_truth"] == 0.8583
          and diag["recovered_vs_truth"] == 240
          and ch["paid_search"]["coverage"] == 0.6176
          and ch["paid_social"]["coverage"] == 0.8049
          and all(row["deduped"] == row["ground_truth"]
                  for row in ch.values()))

    # E6: fallback window behaviour pinned (edge merges, outside does not)
    ident = [{"anon_id": "ANON-E1", "user_id": "HH-U-E1",
              "timestamp": "2026-01-10T11:00:00Z"}]
    edge = reconcile([_pixel(user="HH-U-E1")],
                     [_server(eid=None, ts="2026-01-10T12:15:00Z")], ident)
    outside = reconcile([_pixel(user="HH-U-E1")],
                        [_server(eid=None, ts="2026-01-10T12:15:01Z")], ident)
    check("E6 fallback window pinned (900s edge merges, 901s does not)",
          len(edge["unified"]) == 1
          and edge["unified"][0]["match_reason"] == "MERGED_FALLBACK_WINDOW"
          and len(outside["unified"]) == 2)

    # E7: consent gate drops exactly the non-consented truth, counted
    result = reconcile(ds["pixel"], ds["server"], ds["identifies"])
    gate = apply_consent_gate(result["unified"], ds["consent"])
    nonconsented_truth = sum(1 for r in ds["ground_truth"]
                             if r["consented"] is False)
    consent_by_user = {r["user_id"]: r["consented"] for r in ds["consent"]}
    leaked = [c for c in gate["counted"]
              if c["user_id"] and consent_by_user.get(c["user_id"]) is False]
    check("E7 consent gate exact (27 dropped == non-consented truth, 0 leaks)",
          len(gate["dropped"]) == 27 == nonconsented_truth
          and not leaked
          and all(d["reason_code"] == "DROPPED_NO_CONSENT"
                  for d in gate["dropped"]))

    # E8: deterministic identity only (no identify -> no fallback guess;
    #     exact id still merges without any identity link)
    anon_pixel = _pixel(user=None)
    no_link = reconcile([anon_pixel], [_server(eid=None)], [])
    exact_no_link = reconcile([anon_pixel], [_server(eid="EVT-E1")], [])
    check("E8 deterministic stitching pinned (no identify, no fallback merge)",
          len(no_link["unified"]) == 2 and len(exact_no_link["unified"]) == 1)

    # E9: validation flags planted defects; generated dataset clean
    import copy
    bad = copy.deepcopy(ds)
    bad["pixel"][0]["channel"] = "bogus_ads"
    bad["ground_truth"][0]["revenue"] = -10.0
    bad["ground_truth"].append(copy.deepcopy(bad["ground_truth"][1]))
    bad["consent"][0]["consented"] = "yes"
    planted = validate_dataset(bad)
    kinds = ("invalid channel", "revenue must be >= 0",
             "duplicate conversion_id", "must be a boolean")
    check("E9 validation flags planted defects, generated dataset clean",
          all(any(k in issue for issue in planted) for k in kinds)
          and validate_dataset(ds) == [],
          f"planted_issues={len(planted)}")

    # E10: CLI check exit codes 0 / 1 / 2
    import tempfile
    tmp = tempfile.mkdtemp(prefix="am-evals-")
    good_dir = os.path.join(tmp, "good")
    write_dataset(generate_dataset(conversions=60, seed=42), good_dir)
    bad_dir = os.path.join(tmp, "bad")
    bad_ds = generate_dataset(conversions=10, seed=7)
    bad_ds["pixel"][0]["channel"] = "bogus_ads"
    write_dataset(bad_ds, bad_dir)
    codes = (_run_cli(["check", "--input", good_dir]),
             _run_cli(["check", "--input", good_dir,
                       "--max-reconciliation-error", "-1"]),
             _run_cli(["check", "--input", bad_dir]),
             _run_cli(["check", "--input", "/tmp/no-such-dataset-xyz"]))
    check("E10 CLI check exit codes 0/1/2/2",
          codes == (EXIT_OK, EXIT_FAIL, EXIT_ERROR, EXIT_ERROR), f"codes={codes}")

    # E11: serve round-trip matches local reconcile + report exactly
    small = generate_dataset(conversions=60, seed=42)
    reset_state(small)
    server = make_server(0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/reconcile",
            data=json.dumps({}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            served_rec = json.loads(resp.read())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/report",
                                    timeout=10) as resp:
            served_report = json.loads(resp.read())
        local_rec = reconcile(small["pixel"], small["server"],
                              small["identifies"])
        local_report = build_report(small)
        local_public = {k: v for k, v in local_report.items() if k != "counted"}
        serve_ok = (served_rec["unified"] == json.loads(json.dumps(
                        local_rec["unified"]))
                    and served_report == json.loads(json.dumps(local_public)))
    finally:
        server.shutdown()
        server.server_close()
        reset_state()
    check("E11 serve round-trip matches local reconcile + report", serve_ok)

    # E12: bundled fixture pinned (seed 42, 60 conversions; CSV == JSONL)
    fixture = load_dataset(FIXTURES)
    fx_report = build_report(fixture)
    fx_rec = fx_report["reconciliation"]
    csv_same = (
        load_ground_truth(os.path.join(FIXTURES, "ground_truth.csv"))
        == fixture["ground_truth"]
        and load_events(os.path.join(FIXTURES, "pixel.csv")) == fixture["pixel"]
        and load_events(os.path.join(FIXTURES, "server.csv")) == fixture["server"]
        and load_identifies(os.path.join(FIXTURES, "identifies.csv"))
        == fixture["identifies"]
        and load_consent(os.path.join(FIXTURES, "consent.csv"))
        == fixture["consent"])
    check("E12 fixture pinned (60 truth, naive 111, gated 54, clean, CSV==JSONL)",
          len(fixture["ground_truth"]) == 60 and len(fixture["pixel"]) == 55
          and len(fixture["server"]) == 56
          and fx_rec["naive_count"] == 111 and fx_rec["deduped_count"] == 60
          and fx_rec["consent_gated_count"] == 54
          and validate_dataset(fixture) == [] and csv_same,
          f"naive={fx_rec['naive_count']} gated={fx_rec['consent_gated_count']}")

    # E13: audit completeness + report determinism
    audit_ok = (len(result["audit"]) == rec["naive_count"]
                and all(a["reason_code"] for a in result["audit"])
                and sum(result["reason_counts"].values()) == rec["naive_count"])
    again = build_report(generate_dataset(conversions=240, seed=42))
    deterministic = (json.dumps(report, sort_keys=True)
                     == json.dumps(again, sort_keys=True))
    check("E13 audit covers every raw event once; report deterministic",
          audit_ok and deterministic)

    passed = sum(1 for r in RESULTS if r["passed"])
    total = len(RESULTS)
    summary_doc = {"passed": passed, "total": total,
                   "all_passed": passed == total, "evals": RESULTS}
    with open(os.path.join(HERE, "eval_report.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(summary_doc, sort_keys=True) + "\n")
    print(f"\n{passed}/{total} evals passed -> evals/eval_report.json")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
