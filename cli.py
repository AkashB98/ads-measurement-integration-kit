"""CLI: generate | reconcile | report | check | demo | serve.

SYNTHETIC DATA ONLY — Helios Home is a fictional advertiser and every
dataset handled here is fabricated sample data.

A "dataset" is a directory holding five tables: ``ground_truth``,
``pixel``, ``server``, ``identifies`` and ``consent``, each as
``.jsonl`` or ``.csv``.

Exit codes:
  check : 0 = pass, 1 = a configured threshold breached
          (reconciliation error or match-rate floor),
          2 = usage / IO / data-quality error
  others: 0 = success, 2 = usage / IO error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from generator import generate_dataset, load_dataset, write_dataset  # noqa: E402
from reconcile import apply_consent_gate, reconcile  # noqa: E402
from report import build_report  # noqa: E402
from validation import validate_dataset  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_ERROR = 0, 1, 2
DEFAULT_MAX_RECONCILIATION_ERROR = 0
DEFAULT_MIN_MATCH_RATE = 1.0


def canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True)


def _load_or_error(directory: str) -> Optional[Dict[str, List[Dict]]]:
    try:
        return load_dataset(directory)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
    except (ValueError, OSError) as exc:
        print(f"error: cannot read dataset {directory}: {exc}", file=sys.stderr)
    return None


def _public_report(dataset: Dict[str, List[Dict]]) -> Dict:
    report = build_report(dataset)
    return {k: v for k, v in report.items() if k != "counted"}


def cmd_generate(args) -> int:
    dataset = generate_dataset(conversions=args.count, seed=args.seed)
    fmt = args.format
    if fmt is None:
        fmt = "csv" if args.out_dir.endswith(".csv") else "jsonl"
    try:
        write_dataset(dataset, args.out_dir, fmt=fmt)
    except OSError as exc:
        print(f"error: cannot write {args.out_dir}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"generated synthetic Helios Home dataset (seed={args.seed}): "
          f"{args.count} ground-truth conversions, "
          f"{len(dataset['pixel'])} pixel rows, "
          f"{len(dataset['server'])} server rows -> {args.out_dir}/ ({fmt})")
    return EXIT_OK


def cmd_reconcile(args) -> int:
    dataset = _load_or_error(args.input)
    if dataset is None:
        return EXIT_ERROR
    result = reconcile(dataset["pixel"], dataset["server"],
                       dataset["identifies"])
    gate = apply_consent_gate(result["unified"], dataset["consent"])
    payload = {"unified": result["unified"], "audit": result["audit"],
               "reason_counts": {**result["reason_counts"],
                                 "DROPPED_NO_CONSENT": len(gate["dropped"])},
               "counted": gate["counted"], "dropped": gate["dropped"]}
    if args.limit is not None:
        payload["unified"] = payload["unified"][:args.limit]
    print(canonical(payload))
    return EXIT_OK


def cmd_report(args) -> int:
    dataset = _load_or_error(args.input)
    if dataset is None:
        return EXIT_ERROR
    print(canonical(_public_report(dataset)))
    return EXIT_OK


def cmd_check(args) -> int:
    dataset = _load_or_error(args.input)
    if dataset is None:
        return EXIT_ERROR
    issues = validate_dataset(dataset)
    if issues:
        for issue in issues[:20]:
            print(f"data-quality: {issue}")
        print(f"check FAIL (data quality): {len(issues)} issue(s)")
        return EXIT_ERROR
    report = build_report(dataset)
    rec = report["reconciliation"]
    match_rate = report["diagnostics"]["match_rate_vs_truth"]
    error = abs(rec["reconciliation_error"])
    print(f"naive={rec['naive_count']} deduped={rec['deduped_count']} "
          f"gated={rec['consent_gated_count']} "
          f"truth_consented={rec['ground_truth_consented_count']} "
          f"reconciliation_error={rec['reconciliation_error']} "
          f"match_rate={match_rate} "
          f"max_error={args.max_reconciliation_error} "
          f"min_match_rate={args.min_match_rate}")
    if error > args.max_reconciliation_error:
        print(f"check FAIL: reconciliation error {error} above threshold")
        return EXIT_FAIL
    if match_rate < args.min_match_rate:
        print(f"check FAIL: match rate {match_rate} below floor "
              f"{args.min_match_rate}")
        return EXIT_FAIL
    print("check PASS")
    return EXIT_OK


def cmd_demo(args) -> int:
    import demo as demo_mod
    try:
        demo_mod.main()
    except AssertionError as exc:
        print(f"demo FAILED: {exc}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


# ------------------------------------------------------------------- serve

class _State:
    """Dataset held by the serve process (seeded sample by default)."""
    def __init__(self):
        self.dataset: Dict[str, List[Dict]] = generate_dataset(
            conversions=60, seed=42)


_STATE = _State()


def get_state() -> _State:
    return _STATE


def reset_state(dataset: Optional[Dict[str, List[Dict]]] = None) -> None:
    _STATE.dataset = dataset if dataset is not None else generate_dataset(
        conversions=60, seed=42)


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: Any) -> None:
        self._send(code, (canonical(payload) + "\n").encode(), "application/json")

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            return self._send_json(200, {"status": "ok"})
        if self.path == "/report":
            return self._send_json(200, _public_report(_STATE.dataset))
        return self._send_json(404, {"error": "unknown endpoint"})

    def do_POST(self):  # noqa: N802
        if self.path != "/reconcile":
            return self._send_json(404, {"error": "unknown endpoint"})
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            pixel = body.get("pixel", _STATE.dataset["pixel"])
            server = body.get("server", _STATE.dataset["server"])
            identifies = body.get("identifies", _STATE.dataset["identifies"])
            result = reconcile(pixel, server, identifies)
            return self._send_json(200, {
                "unified": result["unified"], "audit": result["audit"],
                "reason_counts": result["reason_counts"]})
        except (ValueError, TypeError, KeyError) as exc:
            return self._send_json(400, {"error": f"bad request: {exc}"})

    def log_message(self, *a: Any) -> None:
        pass


def make_server(port: int) -> HTTPServer:
    return HTTPServer(("127.0.0.1", port), _Handler)


def cmd_serve(args) -> int:
    if args.input:
        dataset = _load_or_error(args.input)
        if dataset is None:
            return EXIT_ERROR
        reset_state(dataset)
    server = make_server(args.port)
    print(f"serving on http://127.0.0.1:{args.port}  "
          f"(POST /reconcile, GET /report, GET /health)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ads-measurement-integration-kit",
        description="Synthetic pixel + server conversion reconciliation "
                    "(Helios Home sample data).")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="generate a synthetic dataset directory")
    g.add_argument("--count", type=int, default=240,
                   help="number of ground-truth conversions")
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--out-dir", default="dataset")
    g.add_argument("--format", choices=["csv", "jsonl"], default=None)
    g.set_defaults(func=cmd_generate)

    r = sub.add_parser("reconcile", help="dedupe/merge streams + consent gate")
    r.add_argument("--input", required=True, help="dataset directory")
    r.add_argument("--limit", type=int, default=None)
    r.set_defaults(func=cmd_reconcile)

    rp = sub.add_parser("report", help="reconciliation table + diagnostics")
    rp.add_argument("--input", required=True, help="dataset directory")
    rp.set_defaults(func=cmd_report)

    c = sub.add_parser("check", help="CI gate: exit 0 pass / 1 threshold breach / 2 error")
    c.add_argument("--input", required=True, help="dataset directory")
    c.add_argument("--max-reconciliation-error", type=int,
                   default=DEFAULT_MAX_RECONCILIATION_ERROR)
    c.add_argument("--min-match-rate", type=float, default=DEFAULT_MIN_MATCH_RATE)
    c.set_defaults(func=cmd_check)

    sub.add_parser("demo", help="run the end-to-end demo").set_defaults(func=cmd_demo)

    s = sub.add_parser("serve", help="localhost JSON API: POST /reconcile, GET /report")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--input", default=None, help="dataset directory to serve")
    s.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
