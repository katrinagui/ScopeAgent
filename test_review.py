"""Additional review checks. Expected failures are open defects, not passing checks.

Run: python -m unittest test_review -v
No real instruments, credentials, or external services are used.
"""
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

import numpy as np

from devices import discover
from live_stream import LiveMonitor
from scope_agent import ChatClient, Session, parse_json
from scope_analysis import analyze_channel, load_csv, preview_data
from test_devices import FakeManager
from test_ui_server import upload
from ui_server import Workbench, make_server


ROOT = Path(__file__).resolve().parent
CSV = b"time_s,CH1_V\n0,0\n1,1\n2,0\n3,1\n"


class ReviewChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.path = Path(self.temp.name) / "signal.csv"
        self.path.write_bytes(CSV)
        self.book = Workbench(self.temp.name)

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def session(self):
        return Session(self.path)

    def test_sine_frequency_and_rms_across_scales(self):
        for frequency in (50, 1000, 10000):
            for amplitude in (1e-6, .1, 10):
                with self.subTest(frequency=frequency, amplitude=amplitude):
                    t = np.arange(20000) / (frequency * 200)
                    y = amplitude * np.sin(2 * np.pi * frequency * t)
                    result = analyze_channel(t, y)
                    self.assertAlmostEqual(result["timing"]["frequency_hz"], frequency, delta=frequency * 1e-6)
                    self.assertAlmostEqual(result["statistics"]["rms_v"], amplitude / np.sqrt(2), delta=amplitude * 1e-4)
                    json.dumps(result, allow_nan=False)

    def test_pwm_polarity_and_typical_duties(self):
        t = np.arange(20000) / 1e6
        for high_samples in (100, 300, 500, 900):
            y = np.where(np.arange(len(t)) % 1000 < high_samples, 2.3, -1.)
            with self.subTest(high_samples=high_samples):
                result = analyze_channel(t, y)
                self.assertAlmostEqual(result["timing"]["frequency_hz"], 1000, delta=1e-6)
                self.assertAlmostEqual(result["timing"]["duty_cycle_pct"], high_samples / 10, delta=.11)
                self.assertAlmostEqual(result["statistics"]["vpp_v"], 3.3)

    def test_preview_retains_both_single_sample_extremes(self):
        t = np.arange(100000) / 1e6
        y = np.zeros(len(t))
        y[12345], y[12346] = 8., -7.
        result = preview_data(t, {"CH1_V": y})
        points = result["series"][0]["points"]
        self.assertLessEqual(len(points), 2002)
        self.assertIn([t[12345], 8.], points)
        self.assertIn([t[12346], -7.], points)

    def test_csv_bom_crlf_and_input_hash(self):
        import hashlib
        raw = b"\xef\xbb\xbf" + CSV.replace(b"\n", b"\r\n")
        self.path.write_bytes(raw)
        t, channels, digest = load_csv(self.path)
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
        np.testing.assert_array_equal(t, [0, 1, 2, 3])
        self.assertEqual(list(channels), ["CH1_V"])

    def test_json_duplicates_and_nonfinite_are_rejected(self):
        for text in ('{"a":1,"a":2}', '{"a":1e999}', '[NaN]'):
            with self.subTest(prefix=text[:25]), self.assertRaises(ValueError):
                parse_json(text)

    def test_r1_invalid_header_never_persists_credential(self):
        # Deliberately invalid fake credential; connection is only to loopback.
        secret = "review-only-fake-secret"
        with self.assertRaises(ValueError) as error:
            ChatClient("http://127.0.0.1:1/v1", "fake", secret + "\nbad-header")
        self.assertNotIn(secret, str(error.exception))

    @unittest.expectedFailure
    def test_r3_failed_new_report_preserves_existing_records(self):
        for _ in range(3):
            self.book.register(self.session())
        previous = list(self.book.records)
        with patch("ui_server.write_results", side_effect=OSError("injected disk full")):
            with self.assertRaises(OSError):
                self.book.register(self.session())
        self.assertEqual(list(self.book.records), previous)

    @unittest.expectedFailure
    def test_r3_failed_reanalysis_preserves_trace_and_archive(self):
        record = self.book.register(self.session())
        record_id = record["record_id"]
        previous = copy.deepcopy(record)
        archive = self.book.get(record_id)["archive"]
        with patch("ui_server.write_results", side_effect=OSError("injected disk full")):
            with self.assertRaises(OSError):
                self.book.analyze(record_id, {"question": "a different question"})
        self.assertEqual(self.book.get(record_id)["archive"], archive)
        self.assertEqual(self.book.payload(record_id)["trace"], previous["trace"])

    @unittest.expectedFailure
    def test_r5_status_remains_responsive_during_discovery(self):
        entered, release, responded = threading.Event(), threading.Event(), threading.Event()
        manager = FakeManager({})
        def blocked_list(pattern):
            entered.set()
            release.wait(3)
            return ()
        manager.list_resources = blocked_list
        self.book.live.manager_factory = lambda: manager
        scanning = threading.Thread(target=lambda: self.book.live.device_request({}), daemon=True)
        checking = threading.Thread(target=lambda: (self.book.live.status(), responded.set()), daemon=True)
        try:
            scanning.start()
            self.assertTrue(entered.wait(2))
            checking.start()
            responsive = responded.wait(.2)
        finally:
            release.set()
            scanning.join(2)
            checking.join(2)
        self.assertTrue(responsive, "Status is blocked by VISA discovery I/O")

    @unittest.expectedFailure
    def test_r6_truncated_png_is_rejected(self):
        monitor = self.book.live
        truncated = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1).to_bytes(4, "big") * 2
        with self.assertRaises(ValueError):
            monitor._publish({"source": "instrument", "view": "screen"}, threading.Event(), image=truncated)

    @unittest.expectedFailure
    def test_r7_manager_close_failure_becomes_recoverable_error(self):
        manager = FakeManager({})
        def fail_close():
            raise RuntimeError("injected close failure")
        manager.close = fail_close
        with self.assertRaises(ValueError):
            discover(lambda: manager)


class ReviewHTTPChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.server = make_server(self.temp.name, port=0)
        self.errors = io.StringIO()
        self.stderr = patch("sys.stderr", self.errors)
        self.stderr.start()
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        self.server.workbench.close()
        self.stderr.stop()
        self.temp.cleanup()

    def post(self, data):
        request = urllib.request.Request(self.base + "/api/import", data=json.dumps(data).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as error:
            error.read()
            return error.code
        except OSError:
            return "disconnected"

    @unittest.expectedFailure
    def test_r2_long_csv_field_returns_structured_400(self):
        raw = CSV.replace(b"0,0", b"0," + b"1" * 140000, 1)
        self.assertEqual(self.post({"csv": upload("long.csv", raw)}), 400)

    @unittest.expectedFailure
    def test_r2_huge_integer_limit_returns_structured_400(self):
        limits = json.dumps({"CH1_V": {"vpp_v": {"max": 10 ** 400}}}).encode()
        self.assertEqual(self.post({"csv": upload("signal.csv", CSV),
                                    "limits": upload("limits.json", limits)}), 400)

    def test_server_recovers_after_ordinary_invalid_csv(self):
        self.assertEqual(self.post({"csv": upload("bad.csv", b"bad")}), 400)
        self.assertEqual(self.post({"csv": upload("signal.csv", CSV)}), 200)


if __name__ == "__main__":
    unittest.main()
