import csv
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

from scope_agent import ChatClient, Session, model_run, parse_json, write_results
from scope_analysis import analyze_channel


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.csv = self.root / "record.csv"
        t = np.arange(2000) / 100_000
        with self.csv.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["time_s", "CH1_V"])
            writer.writerows(zip(t, np.sin(2 * np.pi * 1000 * t)))

    def config(self, data, name="limits.json"):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_local_end_to_end_with_limits_and_trace(self):
        path = self.config({"CH1_V": {"frequency_hz": {"min": 990, "max": 1010}, "vpp_v": {"max": 2.1}}})
        session = Session(self.csv, limits=path)
        outcome = session.local_run("检查波形")
        output = self.root / "output"
        write_results(session, outcome, output)
        analysis = json.loads((output / "analysis.json").read_text(encoding="utf-8"))
        self.assertEqual(analysis["limit_evaluation"]["status"], "pass")
        self.assertEqual(analysis["channels"]["CH1_V"]["pass_fail"], "pass")
        self.assertEqual(analysis["source"]["sha256"], hashlib.sha256(self.csv.read_bytes()).hexdigest())
        self.assertEqual(len(session.trace), 3)
        self.assertFalse(session.trace[-1]["result"]["execution"])
        self.assertTrue((output / "waveform.svg").is_file())
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            write_results(session, outcome, output)

    def test_interval_uses_original_time_and_sample_count(self):
        session = Session(self.csv)
        result = session.call("inspect_interval", {"channel": "CH1_V", "start_s": .001, "end_s": .002})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result"]["analysis"]["sample_count"], 101)
        self.assertEqual(result["result"]["actual_interval_s"], [.001, .002])
        for start, end in ((-.1, .01), (.01, .001), (0., .000001)):
            result = session.call("inspect_interval", {"channel": "CH1_V", "start_s": start, "end_s": end})
            self.assertEqual(result["status"], "error")

    def test_bad_tool_arguments_and_budget(self):
        session = Session(self.csv, max_calls=6)
        for name, arguments in (("run_scpi", {}), ("analyze_record", {"path": "private.csv"}),
                                ("analyze_record", {"channel": "missing"}),
                                ("inspect_interval", {"channel": "CH1_V", "start_s": True, "end_s": .01}),
                                ("inspect_interval", {"channel": "CH1_V", "start_s": float("nan"), "end_s": .01}),
                                ("analyze_record", [])):
            self.assertEqual(session.call(name, arguments)["status"], "error")
        with self.assertRaisesRegex(ValueError, "budget"):
            session.call("analyze_record", {})

    def test_limit_fail_and_inconclusive_and_unknown(self):
        for bounds, expected in (({"vpp_v": {"max": 1}}, "fail"), ({"frequency_hz": {"min": 1}}, "inconclusive")):
            path = self.config({"CH1_V": bounds})
            if expected == "inconclusive":
                self.csv.write_text("time_s,CH1_V\n0,0\n1,0\n2,0\n3,0\n", encoding="utf-8")
            result = Session(self.csv, limits=path).call("evaluate_limits", {})["result"]
            self.assertEqual(result["status"], expected)
        for limits in ({"bad": {"vpp_v": {"max": 1}}}, {"CH1_V": {"vpp": {"max": 1}}},
                       {"CH1_V": {"vpp_v": {"min": 2, "max": 1}}},
                       {"CH1_V": {"vpp_v": {"max": True}}}):
            with self.assertRaises(ValueError):
                Session(self.csv, limits=self.config(limits))

    def test_reference_zero_and_capture_setup_warning(self):
        reference = self.root / "reference.csv"
        reference.write_text("time_s,CH1_V\n0,0\n1,0\n2,0\n3,0\n", encoding="utf-8")
        result = Session(self.csv, reference=reference).call("compare_reference", {"channel": "CH1_V"})["result"]
        self.assertAlmostEqual(result["metrics"]["vpp_v"]["delta"], 2., places=5)
        self.assertIsNone(result["metrics"]["vpp_v"]["relative_change_pct"])
        self.assertIn("conditions may differ", result["warning"])

    def test_digest_and_analysis_use_same_snapshot_if_source_changes(self):
        original_hash = hashlib.sha256(self.csv.read_bytes()).hexdigest()

        def change_file(t, y):
            self.csv.write_text("time_s,CH1_V\n0,0\n1,0\n2,0\n3,0\n", encoding="utf-8")
            return analyze_channel(t, y)

        with patch("scope_agent.analyze_channel", side_effect=change_file):
            session = Session(self.csv)
        self.assertEqual(session.source["sha256"], original_hash)
        self.assertAlmostEqual(session.analysis["CH1_V"]["statistics"]["vpp_v"], 2.)
        self.assertNotEqual(hashlib.sha256(self.csv.read_bytes()).hexdigest(), original_hash)

    def test_bad_python_arguments_do_not_prevent_persisting_trace(self):
        session = Session(self.csv)
        session.call("inspect_interval", {"channel": "CH1_V", "start_s": float("nan"), "end_s": .01})
        write_results(session, session.local_run("测试无效调用记录"), self.root / "bad-args")
        self.assertIsNone(session.trace[0]["arguments"])

    def test_malformed_model_output_becomes_persistable_error(self):
        class Broken:
            def complete(self, messages):
                return {"content": {"summary": "incorrect type"}}

        session = Session(self.csv)
        outcome = model_run(session, "检查", Broken())
        self.assertEqual(outcome["status"], "error")
        write_results(session, outcome, self.root / "failed-model")
        self.assertTrue((self.root / "failed-model" / "analysis.json").is_file())

    def test_http_redirect_is_not_followed(self):
        requests = []

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.path)
                self.send_response(307)
                self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = ChatClient(f"http://127.0.0.1:{server.server_port}/v1", "fake", "fake-key")
            with self.assertRaisesRegex(ValueError, "redirects are disabled"):
                client.complete([])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.assertEqual(requests, ["/v1/chat/completions"])

    def test_json_rejects_duplicate_and_nonfinite(self):
        for value in ('{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{"x":1,"x":2}'):
            with self.assertRaises(ValueError):
                parse_json(value)

    def test_model_endpoint_validation(self):
        for address in ("http://example.com/v1", "file:///x", "https://user:pass@example.com/v1", "https://example.com/v1?key=x"):
            with self.assertRaises(ValueError):
                ChatClient(address, "test")

    def test_http_model_tool_loop_and_final_citations(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if len(received) == 1:
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call-interval", "type": "function", "function": {
                        "name": "inspect_interval", "arguments": json.dumps({"channel": "CH1_V", "start_s": .001, "end_s": .002})}}]}
                else:
                    message = {"role": "assistant", "content": json.dumps({"summary": "局部记录已复查，仍需确认探头。", "evidence_ids": ["E003"]})}
                body = json.dumps({"choices": [{"message": message}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = ChatClient(f"http://127.0.0.1:{server.server_port}/v1", "fake-test", "fake-key")
            session = Session(self.csv)
            outcome = model_run(session, "检查1到2ms", client)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.assertEqual(outcome["status"], "completed")
        self.assertFalse(outcome["narrative_verified"])
        self.assertEqual(outcome["evidence_ids"], ["E003"])
        self.assertEqual(received[1]["messages"][-1]["tool_call_id"], "call-interval")
        self.assertEqual(session.trace[-1]["result"]["analysis"]["sample_count"], 101)
        output = self.root / "model-output"
        write_results(session, outcome, output)
        self.assertNotIn("fake-key", "".join(p.read_text(encoding="utf-8") for p in output.iterdir()))

    def test_model_budget_and_fabricated_evidence(self):
        class Endless:
            def complete(self, messages):
                return {"tool_calls": [{"id": "again", "type": "function", "function": {"name": "analyze_record", "arguments": "{}"}}]}

        session = Session(self.csv, max_calls=3)
        result = model_run(session, "分析", Endless(), max_rounds=6)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(len(session.trace), 3)

        class Fabricated:
            def complete(self, messages):
                return {"content": '{"summary":"正常","evidence_ids":["E999"]}'}

        result = model_run(Session(self.csv), "分析", Fabricated())
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
