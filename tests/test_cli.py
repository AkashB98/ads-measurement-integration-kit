"""CLI: generate / reconcile / report / check / serve."""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli
from generator import generate_dataset, load_dataset, write_dataset
from reconcile import reconcile
from report import build_report


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestCliCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="am-cli-test-")
        cls.dir = os.path.join(cls.tmp, "data")
        cls.ds = generate_dataset(conversions=60, seed=42)
        write_dataset(cls.ds, cls.dir)

    def test_generate_writes_dataset(self):
        out_dir = os.path.join(self.tmp, "gen")
        code, out, _ = run_cli(["generate", "--count", "25", "--seed", "3",
                                "--out-dir", out_dir])
        self.assertEqual(code, cli.EXIT_OK)
        loaded = load_dataset(out_dir)
        self.assertEqual(len(loaded["ground_truth"]), 25)
        self.assertIn("25 ground-truth conversions", out)

    def test_generate_csv_format(self):
        out_dir = os.path.join(self.tmp, "gen-csv")
        code, _, _ = run_cli(["generate", "--count", "10", "--seed", "3",
                              "--out-dir", out_dir, "--format", "csv"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertTrue(os.path.exists(os.path.join(out_dir, "pixel.csv")))
        self.assertEqual(load_dataset(out_dir),
                         generate_dataset(conversions=10, seed=3))

    def test_reconcile_command(self):
        code, out, _ = run_cli(["reconcile", "--input", self.dir])
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(out)
        expected = reconcile(self.ds["pixel"], self.ds["server"],
                             self.ds["identifies"])
        self.assertEqual(payload["unified"],
                         json.loads(json.dumps(expected["unified"])))
        self.assertEqual(len(payload["audit"]),
                         len(self.ds["pixel"]) + len(self.ds["server"]))

    def test_reconcile_limit(self):
        code, out, _ = run_cli(["reconcile", "--input", self.dir, "--limit", "3"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(len(json.loads(out)["unified"]), 3)

    def test_report_command(self):
        code, out, _ = run_cli(["report", "--input", self.dir])
        self.assertEqual(code, cli.EXIT_OK)
        local = build_report(self.ds)
        local_public = {k: v for k, v in local.items() if k != "counted"}
        self.assertEqual(json.loads(out), json.loads(json.dumps(local_public)))

    def test_missing_input_dir_exit_2(self):
        for argv in (["reconcile", "--input", "/tmp/nope-xyz"],
                     ["report", "--input", "/tmp/nope-xyz"],
                     ["check", "--input", "/tmp/nope-xyz"]):
            code, _, _ = run_cli(argv)
            self.assertEqual(code, cli.EXIT_ERROR, argv)


class TestCheckGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="am-check-test-")
        cls.good = os.path.join(cls.tmp, "good")
        write_dataset(generate_dataset(conversions=60, seed=42), cls.good)

    def test_check_pass_exit_0(self):
        code, out, _ = run_cli(["check", "--input", self.good])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("check PASS", out)

    def test_check_error_breach_exit_1(self):
        code, out, _ = run_cli(["check", "--input", self.good,
                                "--max-reconciliation-error", "-1"])
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertIn("check FAIL", out)

    def test_check_match_rate_breach_exit_1(self):
        code, _, _ = run_cli(["check", "--input", self.good,
                              "--min-match-rate", "1.5"])
        self.assertEqual(code, cli.EXIT_FAIL)

    def test_check_bad_data_exit_2(self):
        bad_dir = os.path.join(self.tmp, "bad")
        ds = generate_dataset(conversions=10, seed=7)
        ds["pixel"][0]["channel"] = "bogus"
        write_dataset(ds, bad_dir)
        code, out, _ = run_cli(["check", "--input", bad_dir])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("data-quality", out)


class TestServe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = generate_dataset(conversions=60, seed=42)
        cli.reset_state(cls.ds)
        cls.server = cli.make_server(0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cli.reset_state()

    def _req(self, path, payload=None, method="GET"):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health(self):
        status, body = self._req("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_report_matches_local(self):
        status, body = self._req("/report")
        self.assertEqual(status, 200)
        local = build_report(self.ds)
        local_public = {k: v for k, v in local.items() if k != "counted"}
        self.assertEqual(body, json.loads(json.dumps(local_public)))

    def test_reconcile_matches_local(self):
        status, body = self._req("/reconcile", payload={}, method="POST")
        self.assertEqual(status, 200)
        local = reconcile(self.ds["pixel"], self.ds["server"],
                          self.ds["identifies"])
        self.assertEqual(body["unified"], json.loads(json.dumps(local["unified"])))

    def test_unknown_endpoint_404(self):
        status, _ = self._req("/nope")
        self.assertEqual(status, 404)
        status, _ = self._req("/report", payload={}, method="POST")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
