from types import SimpleNamespace
import unittest

import numpy as np

from instrument import capture


class FakeScope:
    def __init__(self, points=5, unit="V"):
        self.closed = False
        self.request = None
        self.transferred = False
        self.points, self.unit = points, unit
        self.waveform = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def info(self):
        return SimpleNamespace(vendor="RIGOL", model="MSO5354", firmware="fake")

    def get_channel(self, channel):
        return SimpleNamespace(unit=self.unit, probe_ratio=10., coupling="DC", bandwidth_limit="OFF", scale_per_div=1., offset=0.)

    def acquire(self, channel, mode, *, on_preamble):
        self.request = (channel, mode)
        self.preamble = SimpleNamespace(format=0, type=0, points=self.points, count=1,
            x_increment=.001, x_origin=-.01, x_reference=2., y_increment=.1, y_origin=0., y_reference=0.)
        on_preamble(self.preamble)
        self.transferred = True
        return self

    def values(self, *, start_index, count):
        return (np.arange(start_index, start_index + count) * .1).tolist()


class InstrumentTests(unittest.TestCase):
    def test_explicit_enable_before_connection(self):
        calls = []
        with self.assertRaisesRegex(ValueError, "allow-configuration"):
            capture("fake", scope_factory=lambda *a, **k: calls.append(a))
        self.assertEqual(calls, [])

    def test_time_preamble_scaling_and_no_double_probe_multiplier(self):
        scope = FakeScope()
        t, channels, metadata = capture("fake", channel=2, allow_configuration=True, scope_factory=lambda *a, **k: scope)
        np.testing.assert_allclose(t, [-.012, -.011, -.010, -.009, -.008])
        np.testing.assert_allclose(channels["CH2_V"], [0., .1, .2, .3, .4])
        self.assertEqual(scope.request, ("CHANnel2", "NORMal"))
        self.assertEqual(metadata["probe_ratio"], 10.)
        self.assertFalse(metadata["configuration_restored"])
        self.assertTrue(scope.closed)

    def test_point_limit_rejects_before_transfer_and_closes(self):
        scope = FakeScope(points=100)
        with self.assertRaisesRegex(ValueError, "point limit"):
            capture("fake", allow_configuration=True, max_points=10, scope_factory=lambda *a, **k: scope)
        self.assertFalse(scope.transferred)
        self.assertTrue(scope.closed)

    def test_non_voltage_channel_rejected_before_capture(self):
        scope = FakeScope(unit="A")
        with self.assertRaisesRegex(ValueError, "unit is V"):
            capture("fake", allow_configuration=True, scope_factory=lambda *a, **k: scope)
        self.assertIsNone(scope.request)
        self.assertTrue(scope.closed)


if __name__ == "__main__":
    unittest.main()
