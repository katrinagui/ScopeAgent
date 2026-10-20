import json
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from scope_analysis import analyze_channel, read_csv, report


class Measurements(unittest.TestCase):
    def setUp(self):
        self.t = np.arange(20000) / 1_000_000

    def test_known_sine_measurements(self):
        y = .2 + np.sin(2 * np.pi * 5000 * self.t)
        result = analyze_channel(self.t, y)
        self.assertAlmostEqual(result["statistics"]["vpp_v"], 2., places=5)
        self.assertAlmostEqual(result["statistics"]["mean_v"], .2, delta=.0001)
        self.assertAlmostEqual(result["statistics"]["rms_v"], math.sqrt(.54), delta=.0001)
        self.assertAlmostEqual(result["timing"]["frequency_hz"], 5000, delta=.1)
        self.assertAlmostEqual(result["spectrum"]["dominant_bin_hz"], 5000, delta=1.)

    def test_known_pwm_and_sampling_limited_edges(self):
        y = np.where(np.mod(self.t * 1000, 1) < .3, 3.3, 0.)
        result = analyze_channel(self.t, y)
        self.assertAlmostEqual(result["timing"]["frequency_hz"], 1000, delta=2.)
        self.assertAlmostEqual(result["timing"]["duty_cycle_pct"], 30., delta=.2)
        self.assertEqual(result["rise_time"]["status"], "sample_limited")
        self.assertFalse(result["rise_time"]["physical_edge_time_verified"])

    def test_dc_has_no_invented_frequency(self):
        result = analyze_channel(self.t, np.full(len(self.t), 2.5))
        self.assertIsNone(result["timing"]["frequency_hz"])
        self.assertEqual(result["spectrum"]["status"], "constant_signal")
        self.assertAlmostEqual(result["statistics"]["rms_v"], 2.5)
        self.assertAlmostEqual(result["statistics"]["ac_rms_v"], 0.)

    def test_noise_does_not_establish_a_period(self):
        result = analyze_channel(self.t, np.random.default_rng(7).normal(0, .01, len(self.t)))
        self.assertIsNone(result["timing"]["frequency_hz"])

    def test_nonuniform_record_disables_fft(self):
        t = self.t.copy()
        t[100:] += .001
        result = analyze_channel(t, np.sin(2 * np.pi * 5000 * self.t))
        self.assertFalse(result["uniform_sampling_within_1pct"])
        self.assertEqual(result["spectrum"]["status"], "not_estimated")
        self.assertIsNone(result["timing"]["frequency_hz"])
        self.assertIsNone(result["rise_time"]["observed_s"])

    def test_small_ac_survives_large_dc(self):
        y = 1e7 + .01 * np.sin(2 * np.pi * 5000 * self.t)
        result = analyze_channel(self.t, y)
        self.assertAlmostEqual(result["statistics"]["ac_rms_v"], .01 / math.sqrt(2), delta=1e-6)

    def test_csv_rejects_extra_or_missing_values(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            path = Path(root) / "bad.csv"
            for row in ("0,1,2", "0"):
                path.write_text(f"time_s,CH1_V\n{row}\n1,2\n2,3\n3,4\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "column count"):
                    read_csv(path)

    def test_duplicate_or_reversed_time_is_rejected(self):
        for times in ([0., 1., 1., 2.], [0., 2., 1., 3.]):
            with self.assertRaisesRegex(ValueError, "strictly increase"):
                analyze_channel(times, [0., 1., 0., 1.])

    def test_nonfinite_voltage_is_rejected(self):
        for value in (math.nan, math.inf):
            with self.assertRaisesRegex(ValueError, "Non-finite"):
                analyze_channel([0., 1., 2., 3.], [0., value, 0., 1.])

    def test_sparse_pulse_is_not_declared_constant_or_periodic(self):
        y = np.zeros(len(self.t))
        y[100] = 3.3
        result = analyze_channel(self.t, y)
        self.assertEqual(result["statistics"]["vpp_v"], 3.3)
        self.assertIsNone(result["timing"]["frequency_hz"])
        self.assertTrue(any("sparse pulses" in s for s in result["warnings"]))

    def test_csv_requires_explicit_units(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            path = Path(root) / "input.csv"
            path.write_text("time_ms,CH1\n0,1\n1,2\n2,3\n3,4\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "time_s"):
                read_csv(path)

    def test_report_is_offline_and_preserves_existing_results(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            path = Path(root) / "input.csv"
            path.write_text("time_s,CH1_V\n0,1\n1,2\n2,3\n3,4\n", encoding="utf-8")
            output = Path(root) / "report"
            result = report(path, output)
            encoded = json.loads((output / "analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(encoded["source_sha256"], result["source_sha256"])
            self.assertEqual(encoded["channels"]["CH1_V"]["pass_fail"], "not_evaluated_no_user_limits")
            self.assertTrue((output / "waveform.svg").is_file())
            original = (output / "report.md").read_bytes()
            with self.assertRaisesRegex(ValueError, "never overwritten"):
                report(path, output)
            self.assertEqual((output / "report.md").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
