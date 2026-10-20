import base64
import threading
import time
import unittest

import numpy as np

from live_stream import LiveMonitor, normalize_resource
from test_instrument import FakeScope


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError("Monitor did not reach the expected state before the deadline")


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.monitor = LiveMonitor()

    def tearDown(self):
        self.monitor.close()

    def test_demo_changes_and_snapshot_is_original_copy(self):
        first = self.monitor.start({"interval_ms": 200})
        wait_until(lambda: self.monitor.status()["sequence"] >= 2)
        status = self.monitor.status(include_frame=True)
        self.assertTrue(status["frame"]["synthetic"])
        self.assertGreater(status["received_fps"], 0)
        a = self.monitor.snapshot(first["stream_id"], 1)
        b = self.monitor.snapshot(first["stream_id"], 2)
        self.assertFalse(np.array_equal(a["channels"]["SYNTH_SINE_V"], b["channels"]["SYNTH_SINE_V"]))
        self.assertEqual(len(b["t"]), 5000)
        self.assertTrue(b["metadata"]["live_frame_frozen"])
        b["channels"]["SYNTH_SINE_V"][0] = 999
        self.assertNotEqual(self.monitor.snapshot(first["stream_id"], 2)["channels"]["SYNTH_SINE_V"][0], 999)
        self.monitor.stop(first["stream_id"])
        wait_until(lambda: not self.monitor.status()["worker_alive"])
        sequence = self.monitor.status()["sequence"]
        time.sleep(.22)
        self.assertEqual(self.monitor.status()["sequence"], sequence)

    def test_bad_configuration_never_connects(self):
        calls = []
        self.monitor.scope_factory = lambda *a, **k: calls.append(a)
        invalid = [{"source": "mso5000", "resource": "192.168.1.10"},
                   {"source": "mso5000", "resource": "http://127.0.0.1/", "allow_configuration": True},
                   {"interval_ms": True}, {"interval_ms": 1}, {"channel": 5},
                   {"source": "demo", "view": "screen"}, {"scpi": "RUN"}]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.monitor.start(config)
        self.assertFalse(calls)
        self.assertIsNone(self.monitor.thread)

    def test_lan_resource_validation(self):
        self.assertEqual(normalize_resource(" 192.168.1.10 "), "TCPIP::192.168.1.10::INSTR")
        self.assertEqual(normalize_resource("TCPIP0::192.168.1.10::inst0::INSTR"), "TCPIP0::192.168.1.10::inst0::INSTR")
        self.assertEqual(normalize_resource("TCPIP::host.local::INSTR"), "TCPIP::host.local::INSTR")
        for value in ("999.1.1.1", "USB::INSTR", "TCPIP::bad/host::INSTR", "192.168.1.10;RUN", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_resource(value)

    def test_persistent_curve_session_closes_once_without_control_commands(self):
        scope, connects = FakeScope(), []
        def factory(*args, **kwargs):
            connects.append((args, kwargs))
            return scope
        self.monitor.scope_factory = factory
        config = {"source": "mso5000", "resource": "192.168.1.10", "allow_configuration": True,
                  "channel": 2, "interval_ms": 200}
        started = self.monitor.start(config)
        wait_until(lambda: self.monitor.status()["sequence"] >= 2)
        self.assertEqual(len(connects), 1)
        self.assertEqual(scope.request, ("CHANnel2", "NORMal"))
        self.assertFalse(scope.closed)
        frame = self.monitor.snapshot(started["stream_id"], 1)
        np.testing.assert_allclose(frame["t"], [-.012, -.011, -.010, -.009, -.008])
        np.testing.assert_allclose(frame["channels"]["CH2_V"], [0, .1, .2, .3, .4])
        self.monitor.stop(started["stream_id"])
        wait_until(lambda: not self.monitor.status()["worker_alive"])
        self.assertTrue(scope.closed)
        # FakeScope deliberately has no RUN, STOP, reset, touch or auto-scale methods.

    def test_screen_bytes_and_no_numeric_snapshot(self):
        scope = FakeScope()
        scope.screenshot = lambda: PNG
        self.monitor.scope_factory = lambda *a, **k: scope
        started = self.monitor.start({"source": "mso5000", "resource": "192.168.1.10",
            "allow_configuration": True, "view": "screen"})
        wait_until(lambda: self.monitor.status()["sequence"] > 0)
        frame = self.monitor.status(include_frame=True)["frame"]
        self.assertEqual(base64.b64decode(frame["image_base64"]), PNG)
        self.assertEqual(frame["image_size"], [1, 1])
        self.assertFalse(frame["has_waveform"])
        self.assertIsNone(scope.request)
        with self.assertRaises(ValueError):
            self.monitor.snapshot(started["stream_id"], frame["sequence"])

    def test_read_error_closes_session_without_demo_fallback(self):
        scope = FakeScope()
        scope.screenshot = lambda: b"not a PNG"
        self.monitor.scope_factory = lambda *a, **k: scope
        self.monitor.start({"source": "mso5000", "resource": "192.168.1.10",
            "allow_configuration": True, "view": "screen"})
        wait_until(lambda: not self.monitor.status()["worker_alive"])
        status = self.monitor.status(include_frame=True)
        self.assertEqual(status["state"], "error")
        self.assertIn("PNG", status["error"])
        self.assertIsNone(status["frame"])
        self.assertTrue(scope.closed)

    def test_stop_during_read_prevents_reconnect_and_discards_late_frame(self):
        entered, release = threading.Event(), threading.Event()
        scope = FakeScope()
        def screenshot():
            entered.set()
            release.wait(timeout=2)
            return PNG
        scope.screenshot = screenshot
        self.monitor.scope_factory = lambda *a, **k: scope
        try:
            started = self.monitor.start({"source": "mso5000", "resource": "192.168.1.10",
                "allow_configuration": True, "view": "screen"})
            self.assertTrue(entered.wait(timeout=2))
            with self.assertRaises(ValueError):
                self.monitor.start({})
            with self.assertRaises(ValueError):
                self.monitor.stop("another-session")
            self.monitor.stop(started["stream_id"])
            self.assertEqual(self.monitor.status()["state"], "stopping")
            with self.assertRaises(ValueError):
                self.monitor.start({})
        finally:
            release.set()
        wait_until(lambda: not self.monitor.status()["worker_alive"])
        self.assertEqual(self.monitor.status()["sequence"], 0)
        self.assertTrue(scope.closed)
        restarted = self.monitor.start({})
        self.assertNotEqual(started["stream_id"], restarted["stream_id"])

    def test_frame_history_is_bounded_and_sessions_cannot_mix(self):
        started = self.monitor.start({"interval_ms": 200})
        wait_until(lambda: self.monitor.status()["sequence"] >= 6)
        self.assertLessEqual(len(self.monitor.frames), 5)
        with self.assertRaises(ValueError):
            self.monitor.snapshot(started["stream_id"], 1)
        with self.assertRaises(ValueError):
            self.monitor.snapshot("old-session", self.monitor.status()["sequence"])


if __name__ == "__main__":
    unittest.main()
