"""Offline waveform measurements. Never opens an instrument or calls a model."""
import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import sys

import numpy as np


def load_csv(path):
    # Explicit units prevent silently treating milliseconds or ADC codes as SI.
    with Path(path).open("rb") as stream:
        raw = stream.read(128 * 1024 * 1024 + 1)
    if len(raw) > 128 * 1024 * 1024:
        raise ValueError("CSV exceeds the 128 MiB input limit.")
    with io.StringIO(raw.decode("utf-8-sig"), newline="") as stream:
        reader = csv.DictReader(stream)
        names = reader.fieldnames or []
        channels = [name for name in names if name.endswith("_V")]
        if ("time_s" not in names or not channels or len(set(names)) != len(names)
                or len(names) != len(channels) + 1 or len(channels) > 8):
            raise ValueError("CSV needs a unique time_s header and voltage columns ending in _V.")
        rows = []
        for line, row in enumerate(reader, 2):
            if line > 1_000_001:
                raise ValueError("Prototype limit: 1,000,000 samples per CSV.")
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Wrong column count on CSV line {line}.")
            try:
                rows.append([float(row["time_s"])] + [float(row[name]) for name in channels])
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"Invalid numeric sample on CSV line {line}.") from exc
    if len(rows) < 4:
        raise ValueError("At least four samples are required.")
    values = np.asarray(rows, dtype=float)
    if not np.all(np.isfinite(values)) or np.any(np.diff(values[:, 0]) <= 0):
        raise ValueError("CSV requires finite samples and strictly increasing timestamps.")
    return (values[:, 0], {name: values[:, i + 1] for i, name in enumerate(channels)},
            hashlib.sha256(raw).hexdigest())


def read_csv(path):
    t, channels, _ = load_csv(path)
    return t, channels


def crossings(t, y, level, rising):
    left, right = y[:-1], y[1:]
    mask = ((left < level) & (right >= level)) if rising else ((left > level) & (right <= level))
    indices = np.flatnonzero(mask)
    return t[indices] + (level - left[indices]) * (t[indices + 1] - t[indices]) / (right[indices] - left[indices])


def schmitt_edges(t, y, low, high):
    middle = (low + high) / 2
    lower, upper = low + .35 * (high - low), low + .65 * (high - low)
    state = True if y[0] >= upper else False if y[0] <= lower else None
    pending = None
    rising, falling = [], []
    for i in range(1, len(y)):
        before, current = y[i - 1], y[i]
        if state is None:
            state = True if current >= upper else False if current <= lower else None
            continue
        if not state:
            if before < middle <= current:
                pending = float(t[i - 1] + (middle - before) * (t[i] - t[i - 1]) / (current - before))
            if current <= lower:
                pending = None
            if current >= upper and pending is not None:
                rising.append(pending)
                state, pending = True, None
        else:
            if before > middle >= current:
                pending = float(t[i - 1] + (middle - before) * (t[i] - t[i - 1]) / (current - before))
            if current >= upper:
                pending = None
            if current <= lower and pending is not None:
                falling.append(pending)
                state, pending = False, None
    return np.asarray(rising), np.asarray(falling)


def transition_estimate(t, y, edges, opposite, low, high, rising):
    ten = low + .1 * (high - low)
    ninety = low + .9 * (high - low)
    starts = crossings(t, y, ten if rising else ninety, rising)
    ends = crossings(t, y, ninety if rising else ten, rising)
    durations = []
    for edge in edges:
        start_index = np.searchsorted(starts, edge, side="right") - 1
        end_index = np.searchsorted(ends, edge, side="left")
        if start_index < 0 or end_index == len(ends):
            continue
        start, end = starts[start_index], ends[end_index]
        # Do not pair crossings from different cycles.
        opposite_index = np.searchsorted(opposite, start, side="right")
        if start < edge < end and (opposite_index == len(opposite) or opposite[opposite_index] >= end):
            durations.append(float(end - start))
    if not durations:
        return {"status": "insufficient_data", "observed_s": None}
    duration = float(np.median(durations))
    # Even dense sampling does not remove instrument/probe bandwidth limits.
    limited = duration < 3 * float(np.max(np.diff(t)))
    return {"status": "sample_limited" if limited else "observed_estimate",
            "observed_s": duration, "transitions_used": len(durations),
            "definition": "10–90% of estimated 5th/95th percentile levels",
            "physical_edge_time_verified": False}


def analyze_channel(t, y):
    t, y = np.asarray(t, dtype=float), np.asarray(y, dtype=float)
    if t.ndim != 1 or y.shape != t.shape or len(t) < 4:
        raise ValueError("Time and voltage must be equal-length 1D arrays with >=4 samples.")
    if not np.all(np.isfinite(t)) or not np.all(np.isfinite(y)):
        raise ValueError("Non-finite samples are rejected; missing values are not interpolated away.")
    dt = np.diff(t)
    if not np.all(np.isfinite(dt)) or np.any(dt <= 0):
        raise ValueError("Timestamps must strictly increase; they are never silently sorted.")
    typical_dt = float(np.median(dt))
    uniform = bool(np.max(np.abs(dt - typical_dt)) <= .01 * typical_dt)
    duration = float(t[-1] - t[0])
    # Scale moments to avoid overflow; integrate AC directly to avoid DC cancellation.
    scale = max(float(np.max(np.abs(y))), 1.)
    normalized = y / scale
    mean = scale * float(np.sum((dt / duration) * (normalized[:-1] + normalized[1:]) / 2))
    rms = scale * math.sqrt(float(np.sum((dt / duration) * (normalized[:-1] ** 2 + normalized[1:] ** 2) / 2)))
    ac = y - mean
    ac_scale = max(float(np.max(np.abs(ac))), 1.)
    ac = ac / ac_scale
    ac_rms = ac_scale * math.sqrt(float(np.sum((dt / duration) * (ac[:-1] ** 2 + ac[1:] ** 2) / 2)))
    low, high = (float(v) for v in np.percentile(y, [5, 95]))
    swing = high - low
    noise_proxy = float(np.median(np.abs(np.diff(y))) / .9538725524)
    tolerance = 1e-12 * max(1., float(np.max(np.abs(y))))
    warnings = ["ADC range, probe calibration, analog bandwidth and acquisition gaps outside this record are unknown."]
    if not uniform:
        warnings.append("Nonuniform timestamps: timing and FFT withheld; statistics assume linear behavior across gaps.")
    timing_eligible = uniform and swing > max(6 * noise_proxy, tolerance)
    rising, falling = schmitt_edges(t, y, low, high) if timing_eligible else (np.array([]), np.array([]))
    periods = np.diff(rising)
    stable = len(periods) >= 3 and float(np.std(periods) / np.mean(periods)) < .1
    timing = {"status": "periodic_candidate" if stable else "insufficient_or_nonperiodic",
              "rising_edges": len(rising), "falling_edges": len(falling),
              "complete_periods": len(periods), "frequency_hz": None, "duty_cycle_pct": None,
              "period_variation_observed_s": None, "short_pulse_candidates": 0}
    if len(periods):
        timing["period_variation_observed_s"] = float(np.std(periods))
        timing["median_period_s"] = float(np.median(periods))
        median_period = float(np.median(periods))
        unusual = np.flatnonzero((periods > 1.5 * median_period) | (periods < .5 * median_period))
        timing["unusual_period_indices"] = [int(i) for i in unusual[:30]]
        timing["unusual_period_intervals_s"] = [[float(rising[i]), float(rising[i + 1])] for i in unusual[:8]]
        if len(unusual):
            warnings.append("Uneven threshold-crossing intervals observed; not proof of missing pulses or a hardware fault.")
    if stable:
        period = float(np.median(periods))
        timing["frequency_hz"] = 1 / period
        widths, duties = [], []
        for i in range(len(rising) - 1):
            j = int(np.searchsorted(falling, rising[i]))
            if j < len(falling) and falling[j] < rising[i + 1]:
                width = float(falling[j] - rising[i])
                widths.append(width)
                duties.append(width / float(rising[i + 1] - rising[i]))
        if duties:
            timing["duty_cycle_pct"] = float(np.median(duties) * 100)
            timing["median_high_width_s"] = float(np.median(widths))
            timing["short_pulse_candidates"] = sum(w < 3 * typical_dt for w in widths)
        if period < 20 * typical_dt:
            warnings.append("Fewer than 20 samples per observed cycle: timing/shape measurements have low resolution.")
    if not timing_eligible:
        warnings.append("No well-separated robust levels: timing metrics withheld (DC, noise, sparse pulses, or complex signal possible).")
    elif not stable:
        warnings.append("No stable period established from at least three complete intervals.")
    spectrum = {"status": "not_estimated", "dominant_bin_hz": None}
    if uniform and len(y) >= 32 and swing > tolerance:
        window = np.hanning(len(y))
        power = np.abs(np.fft.rfft((y - np.mean(y)) * window)) ** 2
        power[0] = 0
        if np.any(power > 0):
            peak = int(np.argmax(power))
            df = 1 / (len(y) * typical_dt)
            spectrum = {"status": "windowed_fft_bin_estimate", "dominant_bin_hz": peak * df,
                        "bin_spacing_hz": df, "nyquist_hz": 1 / (2 * typical_dt),
                        "window": "Hann", "is_fundamental_verified": False}
    if np.all(y == y[0]):
        spectrum = {"status": "constant_signal", "dominant_bin_hz": None}
    transition = {"status": "insufficient_data", "observed_s": None}
    return {
        "sample_count": len(y), "duration_s": duration, "median_sample_interval_s": typical_dt,
        "uniform_sampling_within_1pct": uniform, "largest_sample_gap_s": float(np.max(dt)),
        "statistics": {"min_v": float(np.min(y)), "max_v": float(np.max(y)),
                       "vpp_v": float(np.ptp(y)), "mean_v": mean, "rms_v": rms,
                       "ac_rms_v": ac_rms,
                       "method": "time-weighted trapezoidal sampled moments"},
        "estimated_levels": {"low_v": low, "high_v": high, "method": "5th/95th percentiles"},
        "timing": timing, "spectrum": spectrum,
        "rise_time": transition_estimate(t, y, rising, falling, low, high, True) if stable else transition.copy(),
        "fall_time": transition_estimate(t, y, falling, rising, low, high, False) if stable else transition.copy(),
        "warnings": warnings, "pass_fail": "not_evaluated_no_user_limits",
    }


def preview_data(t, channels, start=None, end=None):
    """Bounded min/max envelope for display; never used for measurements."""
    start = float(t[0]) if start is None else start
    end = float(t[-1]) if end is None else end
    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not math.isfinite(start) or not math.isfinite(end) or not t[0] <= start < end <= t[-1]):
        raise ValueError("显示范围必须在原始记录内，且开始时间小于结束时间。")
    left = int(np.searchsorted(t, start, side="left"))
    right = int(np.searchsorted(t, end, side="right"))
    if right - left < 2:
        raise ValueError("显示范围内至少需要两个采样点。")
    series = []
    for name, y in channels.items():
        step = max(1, math.ceil((right - left) / 1000))
        indices = {left, right - 1}
        for i in range(left, right, step):
            chunk = y[i:min(right, i + step)]
            indices.update((i + int(np.argmin(chunk)), i + int(np.argmax(chunk))))
        indices = np.asarray(sorted(indices))
        series.append({"name": name, "points": np.column_stack((t[indices], y[indices])).tolist()})
    return {"start_s": start, "end_s": end, "sample_count": right - left, "series": series}


def write_preview(t, channels, target):
    # ponytail: diagnostic SVG preview, not a zoomable oscilloscope UI.
    # Min/max envelope selection retains narrow excursions in the displayed record.
    import html
    width, panel_height = 1100, 240
    height = panel_height * len(channels) + 35
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#101820"/>',
             '<text x="70" y="23" fill="white" font-family="sans-serif" font-size="16">Diagnostic preview — min/max envelope; all samples used for analysis</text>']
    for row, (name, y) in enumerate(channels.items()):
        top, bottom = row * panel_height + 60, row * panel_height + 205
        lo, hi = float(np.min(y)), float(np.max(y))
        pad = (hi - lo) * .08 if hi > lo else max(abs(lo) * .1, 1.)
        lo, hi = lo - pad, hi + pad
        indices = []
        for start in range(0, len(y), max(1, math.ceil(len(y) / 900))):
            end = min(len(y), start + max(1, math.ceil(len(y) / 900)))
            chunk = y[start:end]
            indices.extend(sorted({start + int(np.argmin(chunk)), start + int(np.argmax(chunk))}))
        indices = sorted(set([0, *indices, len(y) - 1]))
        points = " ".join(f"{70 + (t[i]-t[0])/(t[-1]-t[0])*1000:.2f},{bottom-(y[i]-lo)/(hi-lo)*(bottom-top):.2f}" for i in indices)
        parts += [f'<text x="70" y="{top-13}" fill="#6de0c3" font-family="sans-serif" font-size="15">{html.escape(name)} (V)</text>',
                  f'<path d="M70,{top} V{bottom} H1070" stroke="#78909c" fill="none"/>',
                  f'<polyline points="{points}" fill="none" stroke="#6de0c3" stroke-width="1"/>',
                  f'<text x="5" y="{top+8}" fill="white" font-size="12">{hi:.4g}</text>',
                  f'<text x="5" y="{bottom}" fill="white" font-size="12">{lo:.4g}</text>',
                  f'<text x="70" y="{bottom+23}" fill="white" font-size="12">{t[0]:.6g} s</text>',
                  f'<text x="960" y="{bottom+23}" fill="white" font-size="12">{t[-1]:.6g} s</text>']
    parts.append("</svg>")
    target.write_text("\n".join(parts), encoding="utf-8")


def report(csv_path, output):
    t, channels, digest = load_csv(csv_path)
    result = {"schema_version": 1, "source_csv": Path(csv_path).name,
              "source_sha256": digest,
              "source_kind": "offline_csv_unverified_acquisition",
              "analysis_scope": "sampled-record measurements, not instrument certification or root-cause diagnosis",
              "channels": {name: analyze_channel(t, y) for name, y in channels.items()},
              "next_steps": ["Confirm units, probe attenuation, coupling, bandwidth, trigger and capture mode.",
                             "Use retained raw samples and an appropriate timebase for suspected narrow events.",
                             "Compare simultaneous channels in a shared acquisition before inferring event order."]}
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory must be empty; existing results are never overwritten.")
    output.mkdir(parents=True, exist_ok=True)
    (output / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    write_preview(t, channels, output / "waveform.svg")
    lines = ["# ScopeAgent — 离线分析记录", "", "![波形预览](waveform.svg)", "",
             "来源是导入 CSV，采集配置尚未验证；未连接仪器，未调用远端模型。", "",
             "| 通道 | Vpp (V) | 均值 (V) | RMS (V) | 周期候选频率 (Hz) | 阈值占空比 (%) |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, data in result["channels"].items():
        stats, timing = data["statistics"], data["timing"]
        f = timing["frequency_hz"]
        duty = timing["duty_cycle_pct"]
        lines.append(f"| {name} | {stats['vpp_v']:.6g} | {stats['mean_v']:.6g} | {stats['rms_v']:.6g} | {f'{f:.6g}' if f is not None else '未建立'} | {f'{duty:.6g}' if duty is not None else '未建立'} |")
    lines += ["", "## 判断边界", "",
              "没有用户定义的测试限值，因此没有给出正常/异常或合格/不合格结论。",
              "频率来自稳定阈值交叉间隔；FFT 峰不自动等于基频。正弦波的阈值占空比也不代表它是方波。",
              "上升下降时间见 analysis.json；sample_limited 表示采样限制，不能当真实物理边沿时间。",
              "", "## 数据与测量提醒", ""]
    for name, data in result["channels"].items():
        lines.append(f"### {name}")
        lines += [""] + [f"- {warning}" for warning in data["warnings"]] + [""]
    lines += ["## 建议补齐", ""] + [f"- {step}" for step in result["next_steps"]]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    analyze = sub.add_parser("analyze", help="Analyze normalized SI-unit CSV.")
    analyze.add_argument("csv", type=Path)
    analyze.add_argument("--output", type=Path, required=True)
    demo = sub.add_parser("demo", help="Create explicitly synthetic examples; no hardware.")
    demo.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "demo":
            if args.output.exists() and any(args.output.iterdir()):
                raise ValueError("Demo output must be empty.")
            args.output.mkdir(parents=True, exist_ok=True)
            csv_path = args.output / "synthetic_waveforms.csv"
            t = np.arange(20000) / 1_000_000
            sine = .2 + np.sin(2 * np.pi * 5000 * t)
            square = np.where(np.arange(len(t)) % 1000 < 300, 3.3, 0.)
            with csv_path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["time_s", "SYNTH_SINE_V", "SYNTH_PWM_V"])
                writer.writerows(zip(t, sine, square))
            result = report(csv_path, args.output / "analysis")
            (args.output / "SYNTHETIC_DATA.txt").write_text(
                "Synthetic data, not measured. CH1: 5 kHz sine, 2 Vpp, 0.2 V offset. "
                "CH2: 1 kHz PWM, 3.3 V, nominal 30% duty. Sampling: 1 MSa/s.\n", encoding="utf-8")
        else:
            result = report(args.csv, args.output)
        print(json.dumps({"status": "ok", "channels": list(result["channels"]),
                          "output": str(args.output.resolve()), "instrument_connected": False}, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
