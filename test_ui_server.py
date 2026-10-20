import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

from ui_server import make_server, main
from test_live_stream import wait_until
from test_devices import FakeManager, TEK


def upload(name, raw):
    return {"name": name, "content": base64.b64encode(raw).decode("ascii")}


class WorkbenchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.server = make_server(self.temp.name, port=0)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        self.server.workbench.close()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def demo(self):
        status, body, _ = self.request("/api/demo")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_demo_preview_and_report_archive(self):
        record = self.demo()
        self.assertTrue(record["demo"])
        self.assertEqual(record["limits"]["status"], "pass")
        record_id = record["record_id"]
        status, body, _ = self.request(f"/api/plot?record_id={record_id}&start_s=0&end_s=0.002")
        plot = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(plot["sample_count"], 2001)
        self.assertLessEqual(len(plot["series"][0]["points"]), 2002)
        status, body, headers = self.request(f"/api/report?record_id={record_id}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertEqual(set(archive.namelist()), {"analysis.json", "agent.json", "trace.jsonl", "report.md", "waveform.svg"})
            self.assertEqual(json.loads(archive.read("agent.json"))["mode"], "local_rules")
        self.assertEqual(self.demo()["record_id"], record_id)

    def test_upload_reference_limits_and_snapshot_hash(self):
        raw = b"time_s,CH4_V\n0,0\n1,1\n2,0\n3,1\n"
        reference = b"time_s,CH4_V\n0,0\n1,0\n2,0\n3,0\n"
        limits = b'{"CH4_V":{"vpp_v":{"min":0.9,"max":1.1}}}'
        status, body, _ = self.request("/api/import", {"csv": upload("../my-signal.csv", raw),
            "reference": upload("good.csv", reference), "limits": upload("limits.json", limits)})
        record = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(record["demo"])
        self.assertTrue(record["has_reference"])
        self.assertEqual(record["source"]["name"], "my-signal.csv")
        self.assertEqual(record["source"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(record["channels"]["CH4_V"]["pass_fail"], "pass")
        self.assertTrue(any(e["tool"] == "compare_reference" for e in record["trace"]))
        self.assertFalse((Path(self.temp.name) / "my-signal.csv").exists())

    def test_interval_recheck_and_invalid_request_preserves_result(self):
        record = self.demo()
        status, body, _ = self.request("/api/analyze", {"record_id": record["record_id"],
            "interval": {"channel": "SYNTH_SINE_V", "start_s": .001, "end_s": .002}})
        result = json.loads(body)
        self.assertEqual(status, 200)
        recheck = next(e for e in result["trace"] if e["tool"] == "inspect_interval")
        self.assertEqual(recheck["result"]["analysis"]["sample_count"], 1001)
        self.assertAlmostEqual(recheck["result"]["analysis"]["timing"]["frequency_hz"], 5000)
        status, _, _ = self.request("/api/analyze", {"record_id": record["record_id"],
            "interval": {"channel": "SYNTH_SINE_V", "start_s": 2, "end_s": 1}})
        self.assertEqual(status, 400)
        self.assertEqual(self.demo()["trace"], result["trace"])

    def test_invalid_upload_is_recoverable_and_not_retained(self):
        record = self.demo()
        status, body, _ = self.request("/api/import", {"csv": upload("bad.csv", b"time_ms,CH1\n0,1\n1,2\n2,3\n3,4\n")})
        self.assertEqual(status, 400)
        self.assertIn("time_s", json.loads(body)["error"])
        self.assertEqual(len(self.server.workbench.records), 1)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.assertEqual(self.demo()["record_id"], record["record_id"])

    def test_local_origin_static_allowlist_and_upload_bound(self):
        self.assertEqual(self.request("/api/demo", headers={"Host": "untrusted.example"})[0], 403)
        self.assertEqual(self.request("/api/demo", headers={"Origin": "https://untrusted.example"})[0], 403)
        self.assertNotEqual(self.request("/../../scope_agent.py")[0], 200)
        status, body, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b'lang="zh-CN"', body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/api/import", {}, {"Content-Length": "50331649"})[0], 413)

    def test_record_retention_releases_old_uploaded_files(self):
        raw = b"time_s,CH1_V\n0,0\n1,0\n2,0\n3,0\n"
        ids = []
        for _ in range(4):
            status, body, _ = self.request("/api/import", {"csv": upload("signal.csv", raw)})
            self.assertEqual(status, 200)
            ids.append(json.loads(body)["record_id"])
        self.assertEqual(len(self.server.workbench.records), 3)
        self.assertEqual(len(list(Path(self.temp.name).glob("record-*"))), 3)
        self.assertEqual(self.request(f"/api/report?record_id={ids[0]}")[0], 400)

    def test_live_demo_freeze_report_and_reimport_preserve_displayed_frame(self):
        status, body, _ = self.request("/api/live/start", {"source": "demo", "interval_ms": 200})
        self.assertEqual(status, 200)
        stream_id = json.loads(body)["stream_id"]
        wait_until(lambda: self.server.workbench.live.status()["sequence"] >= 2)
        status, body, _ = self.request("/api/live/frame")
        frame = json.loads(body)["frame"]
        self.assertTrue(frame["synthetic"])
        status, body, _ = self.request("/api/live/snapshot", {"stream_id": stream_id, "sequence": frame["sequence"]})
        self.assertEqual(status, 200)
        record = json.loads(body)
        self.assertTrue(record["demo"])
        self.assertEqual(record["metadata"]["live_frame_sequence"], frame["sequence"])
        self.assertEqual(record["sample_count"], 5000)
        self.assertEqual(record["limits"]["status"], "not_evaluated")
        for name in frame["channels"]:
            self.assertEqual(record["channels"][name]["statistics"], frame["channels"][name]["statistics"])
        _, raw_zip, _ = self.request(f"/api/report?record_id={record['record_id']}")
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            raw = archive.read("live-frame.csv")
            self.assertEqual(hashlib.sha256(raw).hexdigest(), record["source"]["sha256"])
            metadata = archive.read("metadata.json")
            self.assertEqual(json.loads(metadata)["live_frame_sequence"], frame["sequence"])
        status, body, _ = self.request("/api/import", {"csv": upload("frozen.csv", raw), "metadata": upload("metadata.json", metadata)})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["channels"], record["channels"])
        wait_until(lambda: not self.server.workbench.live.status()["worker_alive"])

    def test_live_routes_bypass_analysis_lock_and_reject_bad_requests(self):
        self.assertEqual(self.request("/api/live/start", {}, {"Origin": "https://untrusted.example"})[0], 403)
        with self.server.workbench.lock:
            status, body, _ = self.request("/api/live/start", {"source": "demo"})
            self.assertEqual(status, 200)
            stream = json.loads(body)["stream_id"]
            self.assertEqual(self.request("/api/live/frame")[0], 200)
            self.assertEqual(self.request("/api/live/stop", {"stream_id": stream})[0], 200)
        wait_until(lambda: not self.server.workbench.live.status()["worker_alive"])
        self.assertEqual(self.request("/api/live/stop", {"stream_id": "old"})[0], 400)
        self.assertEqual(self.request("/api/live/snapshot", {})[0], 400)
        self.assertEqual(self.request("/live.js")[0], 200)
        self.assertEqual(self.request("/live.css")[0], 200)

    def test_device_selection_routes_and_unsupported_model_guard(self):
        self.server.workbench.live.manager_factory = FakeManager
        self.assertEqual(self.request("/api/devices", {}, {"Origin": "https://untrusted.example"})[0], 403)
        self.assertEqual(self.request("/api/devices", {"command": "RUN"})[0], 400)
        status, body, _ = self.request("/api/devices", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["devices"]), 2)
        status, body, _ = self.request("/api/devices", {"resource": "192.168.1.10"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["devices"][0]["model"], "MSO5354")
        self.assertEqual(self.request("/api/live/start", {"source": "instrument", "resource": TEK,
            "allow_configuration": True})[0], 400)
        self.assertEqual(self.request("/api/live/start", {"source": "instrument", "resource": "192.168.1.99",
            "allow_configuration": True})[0], 400)

    def test_model_settings_enable_analysis_without_exposing_or_persisting_key(self):
        with patch.dict("os.environ", {"SCOPE_BASE_URL": "", "SCOPE_MODEL": "", "SCOPE_API_KEY": ""}):
            record = self.demo()
            self.assertFalse(record["llm_available"])
            settings = {"base_url": "http://127.0.0.1:1/v1", "model": "review-model", "api_key": "fake-session-key"}
            with patch("scope_agent.ChatClient.complete", return_value={"content": '{"summary":"模拟模型说明","evidence_ids":["E001"]}'}) as complete:
                status, body, _ = self.request("/api/model", settings)
                self.assertEqual(status, 200)
                complete.assert_not_called()
                self.assertTrue(json.loads(body)["configured"])
                self.assertNotIn(b"fake-session-key", body)
                self.assertNotIn(b"fake-session-key", self.request("/api/model")[1])
                self.assertEqual(list(Path(self.temp.name).iterdir()), [])
                status, body, _ = self.request("/api/analyze", {"record_id": record["record_id"], "mode": "llm"})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["outcome"]["status"], "completed")
                complete.assert_called_once()
            archive = self.request(f"/api/report?record_id={record['record_id']}")[1]
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                self.assertTrue(all(b"fake-session-key" not in zipped.read(name) for name in zipped.namelist()))
            self.assertEqual(self.request("/api/model", {"enabled": False})[0], 200)
            self.assertFalse(json.loads(self.request("/api/model")[1])["configured"])
            self.assertEqual(self.request("/api/analyze", {"record_id": record["record_id"], "mode": "llm"})[0], 400)

    def test_invalid_model_settings_preserve_previous_config_and_origin_guard(self):
        settings = {"base_url": "http://127.0.0.1:1/v1", "model": "fake", "api_key": "fake-valid-key"}
        self.assertEqual(self.request("/api/model", settings)[0], 200)
        self.assertEqual(self.request("/api/model", settings, {"Origin": "https://untrusted.example"})[0], 403)
        for changes in ({"api_key": "fake-secret\ninvalid"}, {"api_key": "密钥"}, {"base_url": "http://example.com/v1"},
                        {"model": ""}, {"api_key": 123}, {"enabled": 0}):
            data = changes if "enabled" in changes else {**settings, **changes}
            with self.subTest(changes=changes):
                status, body, _ = self.request("/api/model", data)
                self.assertEqual(status, 400)
                self.assertNotIn(b"fake-secret", body)
                self.assertEqual(self.server.workbench.model_config(), settings)
        self.assertEqual(self.request("/api/model", {**settings, "api_key": ""})[0], 200)
        self.assertFalse(json.loads(self.request("/api/model")[1])["has_api_key"])

    def test_environment_model_settings_are_read_only_and_invalid_secrets_not_reflected(self):
        with patch.dict("os.environ", {"SCOPE_BASE_URL": "https://provider.example/v1", "SCOPE_MODEL": "env-model", "SCOPE_API_KEY": "fake-env-key"}):
            body = self.request("/api/model")[1]
            self.assertTrue(json.loads(body)["configured"])
            self.assertEqual(json.loads(body)["source"], "environment")
            self.assertNotIn(b"fake-env-key", body)
            self.request("/api/model", {"enabled": False})
            self.assertFalse(json.loads(self.request("/api/model")[1])["configured"])
        self.server.workbench.model_override = None
        with patch.dict("os.environ", {"SCOPE_BASE_URL": "https://user:fake-url-secret@provider.example/v1", "SCOPE_MODEL": "env"}):
            body = self.request("/api/model")[1]
            self.assertFalse(json.loads(body)["configured"])
            self.assertNotIn(b"fake-url-secret", body)

    def test_browser_entry_reuses_only_a_running_scopeagent(self):
        status, body, _ = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["app"], "ScopeAgent")
        with patch("sys.argv", ["ui_server.py", "--port", str(self.server.server_port), "--open-browser"]), \
                patch("ui_server.webbrowser.open") as opened, patch("ui_server.make_server") as create:
            main()
        create.assert_not_called()
        opened.assert_called_once_with(self.base)

    def test_browser_entry_starts_service_and_closes_on_interrupt(self):
        with patch("sys.argv", ["ui_server.py", "--open-browser"]), \
                patch("ui_server.urllib.request.urlopen", side_effect=OSError), \
                patch("ui_server.webbrowser.open") as opened, patch("ui_server.make_server") as create:
            create.return_value.server_port = 8765
            create.return_value.serve_forever.side_effect = KeyboardInterrupt
            main()
        opened.assert_called_once_with("http://127.0.0.1:8765")
        create.return_value.server_close.assert_called_once()
        create.return_value.workbench.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
