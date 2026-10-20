from types import SimpleNamespace
import unittest

from devices import discover, identify, normalize_resource
from live_stream import LiveMonitor
from test_instrument import FakeScope
from test_live_stream import wait_until


RIGOL = "TCPIP::192.168.1.10::INSTR"
TEK = "USB0::0x0699::0x0408::TEK123::INSTR"


class FakeResource:
    def __init__(self, reply):
        self.reply, self.closed, self.commands = reply, False, []

    def query(self, command):
        self.commands.append(command)
        if command != "*IDN?":
            raise AssertionError("Only identity may be queried")
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def close(self):
        self.closed = True


class FakeManager:
    def __init__(self, replies=None):
        self.replies = replies if replies is not None else {
            RIGOL: "RIGOL,MSO5354,SCOPE123,FAKE1.0\n",
            TEK: "TEKTRONIX,TBS1102B,TEK123,FAKE2.0\n"}
        self.closed, self.opened = False, []

    def list_resources(self, pattern):
        if pattern != "?*::INSTR":
            raise AssertionError("Unexpected discovery scope")
        return tuple(self.replies)

    def open_resource(self, resource, **kwargs):
        if kwargs != {"timeout": 1000, "read_termination": "\n", "write_termination": "\n"}:
            raise AssertionError("Identity I/O must be bounded and terminated")
        device = FakeResource(self.replies[resource])
        self.opened.append(device)
        return device

    def close(self):
        self.closed = True


class DeviceTests(unittest.TestCase):
    def test_scan_reads_actual_identity_and_marks_unsupported_models(self):
        manager = FakeManager()
        result = discover(lambda: manager)
        self.assertEqual(len(result["devices"]), 2)
        rigol = next(d for d in result["devices"] if d["vendor"] == "RIGOL")
        tek = next(d for d in result["devices"] if d["vendor"] == "TEKTRONIX")
        self.assertTrue(rigol["supported"])
        self.assertEqual(rigol["serial"], "SCOPE123")
        self.assertEqual(rigol["channels"], [1, 2, 3, 4])
        self.assertFalse(tek["supported"])
        self.assertEqual(tek["views"], [])
        self.assertTrue(manager.closed)
        self.assertTrue(all(d.closed and d.commands == ["*IDN?"] for d in manager.opened))

    def test_manual_identity_two_channel_model_and_resource_validation(self):
        manager = FakeManager({RIGOL: "RIGOL,MSO5072,SCOPE2,FAKE"})
        device = identify("192.168.1.10", lambda: manager)
        self.assertEqual(device["channels"], [1, 2])
        incomplete = identify(RIGOL, lambda: FakeManager({RIGOL: "RIGOL,MSO5354"}))
        self.assertFalse(incomplete["supported"])
        self.assertEqual(normalize_resource(TEK), TEK)
        self.assertEqual(normalize_resource("TCPIP0::scope.local::inst0::INSTR"), "TCPIP0::scope.local::inst0::INSTR")
        for value in ("http://192.168.1.10", "TCPIP::192.168.1.10::5555::SOCKET", "USB::INSTR", "TCPIP::999.1.1.1::INSTR", "TCPIP::bad;RUN::INSTR"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_resource(value)

    def test_scan_handles_read_errors_empty_results_and_bounds_work(self):
        self.assertEqual(discover(lambda: FakeManager({}))["devices"], [])
        manager = FakeManager({f"TCPIP::192.168.1.{i}::INSTR": TimeoutError() for i in range(10, 21)})
        result = discover(lambda: manager)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(manager.opened), 8)
        self.assertTrue(all(not d["supported"] for d in result["devices"]))
        self.assertTrue(manager.closed)
        self.assertTrue(all(d.closed for d in manager.opened))

    def test_malformed_identity_is_rejected_and_connections_close(self):
        for reply in ("garbage", "RIGOL,MSO5354\nRUN", "x" * 4097, None):
            manager = FakeManager({RIGOL: reply})
            with self.subTest(reply=str(reply)[:20]), self.assertRaises(ValueError):
                identify(RIGOL, lambda: manager)
            self.assertTrue(manager.closed)
            self.assertTrue(manager.opened[0].closed)

    def test_live_requires_identification_and_respects_model_channel_count(self):
        scope = FakeScope()
        scope.info = lambda: SimpleNamespace(vendor="RIGOL", model="MSO5072", serial="SCOPE2", firmware="FAKE")
        monitor = LiveMonitor(scope_factory=lambda *a, **k: scope,
            manager_factory=lambda: FakeManager({RIGOL: "RIGOL,MSO5072,SCOPE2,FAKE"}))
        config = {"source": "instrument", "resource": RIGOL, "allow_configuration": True}
        try:
            with self.assertRaisesRegex(ValueError, "先扫描或识别"):
                monitor.start(config)
            monitor.device_request({"resource": RIGOL})
            with self.assertRaisesRegex(ValueError, "能力"):
                monitor.start({**config, "channel": 3})
            monitor.start({**config, "channel": 2})
            wait_until(lambda: monitor.status()["sequence"] > 0)
            self.assertEqual(scope.request, ("CHANnel2", "NORMal"))
            with self.assertRaisesRegex(ValueError, "先停止"):
                monitor.device_request({})
        finally:
            monitor.close()
        self.assertTrue(scope.closed)

    def test_unknown_model_cannot_start_and_changed_identity_never_captures(self):
        scope = FakeScope()
        scope.info = lambda: SimpleNamespace(vendor="TEKTRONIX", model="TBS1102B", firmware="FAKE")
        monitor = LiveMonitor(scope_factory=lambda *a, **k: scope, manager_factory=FakeManager)
        try:
            monitor.device_request({})
            with self.assertRaisesRegex(ValueError, "已有适配器"):
                monitor.start({"source": "instrument", "resource": TEK, "allow_configuration": True})
            self.assertIsNone(scope.request)
            monitor.start({"source": "instrument", "resource": RIGOL, "allow_configuration": True})
            wait_until(lambda: not monitor.status()["worker_alive"])
            self.assertEqual(monitor.status()["state"], "error")
            self.assertIsNone(scope.request)
            self.assertTrue(scope.closed)
        finally:
            monitor.close()

    def test_scan_failure_is_recoverable_and_closes_manager(self):
        manager = FakeManager()
        manager.list_resources = lambda pattern: (_ for _ in ()).throw(RuntimeError("backend failure"))
        with self.assertRaisesRegex(ValueError, "扫描失败"):
            discover(lambda: manager)
        self.assertTrue(manager.closed)


if __name__ == "__main__":
    unittest.main()
