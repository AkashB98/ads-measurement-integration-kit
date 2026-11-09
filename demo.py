"""End-to-end demo: generate -> reconcile -> gate -> report -> serve.

SYNTHETIC DATA ONLY (fictional advertiser Helios Home). Runs fully
offline; the serve round-trip is loopback-only on an ephemeral port.
Every stage asserts.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cli import EXIT_OK, main as cli_main, make_server, reset_state  # noqa: E402
from generator import generate_dataset  # noqa: E402
from reconcile import apply_consent_gate, reconcile  # noqa: E402
from report import build_report  # noqa: E402
from validation import validate_dataset  # noqa: E402


def _request(port: int, path: str, payload=None, method: str = "GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def main() -> None:
    print("== 1. generate synthetic Helios Home dataset (seed=42) ==")
    dataset = generate_dataset(conversions=240, seed=42)
    again = generate_dataset(conversions=240, seed=42)
    assert dataset == again, "generator must be deterministic"
    assert validate_dataset(dataset) == [], validate_dataset(dataset)[:3]
    naive = len(dataset["pixel"]) + len(dataset["server"])
    print(f"   {len(dataset['ground_truth'])} true conversions -> "
          f"{len(dataset['pixel'])} pixel rows + {len(dataset['server'])} "
          f"server rows (naive count {naive}) — deterministic, validates clean")

    print("\n== 2. reconcile: duplicates dropped, pairs merged, singletons kept ==")
    result = reconcile(dataset["pixel"], dataset["server"],
                       dataset["identifies"])
    assert len(result["audit"]) == naive, "every raw event must be audited"
    assert all(a["reason_code"] for a in result["audit"])
    rec = build_report(dataset)["reconciliation"]
    gaps = build_report(dataset)["gaps_explained"]
    assert rec["naive_count"] - rec["deduped_count"] == gaps["check_naive_gap"]
    print(f"   naive {rec['naive_count']} -> deduped {rec['deduped_count']} "
          f"(duplicates {gaps['duplicate_deliveries']}, exact merges "
          f"{gaps['exact_merges']}, fallback merges {gaps['fallback_merges']})")

    print("\n== 3. consent gate: non-consented dropped and counted, never silent ==")
    gate = apply_consent_gate(result["unified"], dataset["consent"])
    assert len(gate["counted"]) + len(gate["dropped"]) == len(result["unified"])
    assert all(d["reason_code"] == "DROPPED_NO_CONSENT" for d in gate["dropped"])
    assert rec["consent_gated_count"] == len(gate["counted"])
    print(f"   deduped {rec['deduped_count']} -> consent-gated "
          f"{rec['consent_gated_count']} ({len(gate['dropped'])} dropped "
          f"no-consent)")

    print("\n== 4. reconciliation vs ground truth ==")
    diag = build_report(dataset)["diagnostics"]
    assert rec["deduped_count"] == rec["ground_truth_count"], rec
    assert rec["consent_gated_count"] == rec["ground_truth_consented_count"], rec
    assert rec["reconciliation_error"] == 0
    assert diag["match_rate_vs_truth"] == 1.0
    print(f"   deduped == ground truth ({rec['ground_truth_count']}); "
          f"gated == consented truth "
          f"({rec['ground_truth_consented_count']}); match rate 1.0, "
          f"duplicate rate {diag['duplicate_rate_vs_truth']}")

    print("\n== 5. CLI check gate exit codes ==")
    import tempfile
    from generator import write_dataset
    tmp = tempfile.mkdtemp(prefix="am-demo-")
    good_dir = os.path.join(tmp, "good")
    write_dataset(generate_dataset(conversions=60, seed=7), good_dir)
    code_ok = cli_main(["check", "--input", good_dir])
    code_fail = cli_main(["check", "--input", good_dir,
                          "--max-reconciliation-error", "-1"])
    code_err = cli_main(["check", "--input", "/tmp/no-such-dataset-xyz"])
    print(f"   pass={code_ok} error-breach={code_fail} missing-dir={code_err}")
    assert (code_ok, code_fail, code_err) == (EXIT_OK, 1, 2)

    print("\n== 6. live localhost serve round-trip ==")
    small = generate_dataset(conversions=60, seed=7)
    reset_state(small)
    server = make_server(0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, health = _request(port, "/health")
        assert status == 200 and health == {"status": "ok"}
        status, served_report = _request(port, "/report")
        local = build_report(small)
        local_public = {k: v for k, v in local.items() if k != "counted"}
        assert status == 200 and served_report == json.loads(
            json.dumps(local_public)), "served report must match local report"
        local_rec = reconcile(small["pixel"], small["server"],
                              small["identifies"])
        status, served_rec = _request(port, "/reconcile", payload={},
                                      method="POST")
        assert status == 200 and served_rec["unified"] == json.loads(
            json.dumps(local_rec["unified"]))
        print("   GET /report matches local report exactly; "
              "POST /reconcile matches local reconcile")
    finally:
        server.shutdown()
        server.server_close()
        reset_state()

    print("\nDemo OK: generate, reconcile, gate, report, check and serve verified.")


if __name__ == "__main__":
    main()
