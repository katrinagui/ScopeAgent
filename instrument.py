"""Explicit, optional MSO5000 voltage capture. No connection occurs on import."""
import argparse
import csv
from datetime import datetime, timezone
import importlib.metadata
import json
import math
from pathlib import Path
import re
import sys

import numpy as np

from scope_analysis import analyze_channel

PREAMBLE_FIELDS = ("format", "type", "points", "count", "x_increment", "x_origin", "x_reference",
                   "y_increment", "y_origin", "y_reference")


def connect_scope(resource, *, timeout_ms):
    if sys.version_info < (3, 14):
        raise ValueError("Hardware capture requires Python >=3.14 and pymso5000==0.2.0; offline analysis supports Python 3.12.")
    try:
        if importlib.metadata.version("pymso5000") != "0.2.0":
            raise ValueError("This adapter targets pymso5000==0.2.0; verify a new API before changing the version.")
        from pymso5000 import Scope
    except ImportError as exc:
        raise ValueError("Optional pymso5000 package is not installed in this interpreter.") from exc
    return Scope.connect(resource, timeout_ms=timeout_ms)


def capture(resource, *, channel=1, mode="NORMal", allow_configuration=False,
            max_points=1_000_000, timeout_ms=10_000, scope_factory=connect_scope):
    if not allow_configuration:
        raise ValueError("Capture changes waveform transfer settings; explicitly enable --allow-configuration.")
    if type(channel) is not int or channel not in (1, 2, 3, 4):
        raise ValueError("Only analog channels 1–4 are supported.")
    if mode not in ("NORMal", "RAW"):
        raise ValueError("Only NORMal and RAW capture modes are supported.")
    if type(max_points) is not int or not 4 <= max_points <= 1_000_000:
        raise ValueError("Point limit must be 4–1,000,000.")
    if type(timeout_ms) is not int or not 100 <= timeout_ms <= 60_000:
        raise ValueError("Timeout must be 100–60,000 ms.")
    if not isinstance(resource, str) or not resource.strip():
        raise ValueError("An explicit VISA resource is required.")

    def validate_preamble(preamble):
        if type(preamble.points) is not int or not 4 <= preamble.points <= max_points:
            raise ValueError("Capture exceeds point limit or contains fewer than four points.")
        for field in PREAMBLE_FIELDS:
            if not math.isfinite(getattr(preamble, field)):
                raise ValueError("Invalid non-finite preamble.")
        if preamble.x_increment <= 0:
            raise ValueError("Preamble sample interval must be positive.")

    # Upstream acquire may alter source/mode, consume error queues, and reject RAW
    # while running. We deliberately never call stop/run or restore guessed settings.
    with scope_factory(resource, timeout_ms=timeout_ms) as scope:
        info = scope.info()
        if (not info.model or not re.fullmatch(r"MSO5\d{3}", info.model.upper())
                or not info.vendor or "RIGOL" not in info.vendor.upper()):
            raise ValueError("This adapter is restricted to identified RIGOL MSO5000 instruments.")
        settings = scope.get_channel(channel)
        if settings.unit != "V":
            raise ValueError("Only channels whose reported engineering unit is V can be exported as _V.")
        waveform = scope.waveform.acquire(f"CHANnel{channel}", mode, on_preamble=validate_preamble)
        preamble = waveform.preamble
        validate_preamble(preamble)
        chunks = [waveform.values(start_index=i, count=min(10_000, preamble.points - i))
                  for i in range(0, preamble.points, 10_000)]
        values = np.concatenate(chunks)
        if len(values) != preamble.points:
            raise ValueError("Transferred length differs from the preamble.")
        t = preamble.x_origin + (np.arange(preamble.points) - preamble.x_reference) * preamble.x_increment
        analyze_channel(t, values)  # Same validation as imported CSV, before writing.
        metadata = {
            "source_kind": "instrument_capture_unverified_calibration", "synthetic": False,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(), "adapter": "pymso5000==0.2.0",
            "instrument": {"vendor": info.vendor, "model": info.model,
                           "serial": getattr(info, "serial", None), "firmware": info.firmware},
            "channel": channel, "unit": settings.unit, "acquisition_mode": mode,
            "probe_ratio": settings.probe_ratio, "coupling": settings.coupling,
            "bandwidth_limit": settings.bandwidth_limit, "scale_per_div": settings.scale_per_div,
            "offset": settings.offset, "preamble": {field: getattr(preamble, field) for field in PREAMBLE_FIELDS},
            "trigger": "unknown", "calibration": "unknown", "configuration_restored": False,
            "notice": "Transfer source/mode and error queue may have changed. RAW requires an already stopped instrument.",
        }
    return t, {f"CH{channel}_V": values}, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--mode", choices=("NORMal", "RAW"), default="NORMal")
    parser.add_argument("--max-points", type=int, default=1_000_000)
    parser.add_argument("--timeout-ms", type=int, default=10_000)
    parser.add_argument("--allow-configuration", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
            raise ValueError("Output directory must be empty; existing results are never overwritten.")
        t, channels, metadata = capture(args.resource, channel=args.channel, mode=args.mode,
            allow_configuration=args.allow_configuration, max_points=args.max_points, timeout_ms=args.timeout_ms)
        args.output.mkdir(parents=True, exist_ok=True)
        with (args.output / "capture.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["time_s", *channels])
            writer.writerows(zip(t, *channels.values()))
        (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({"status": "captured", "points": len(t), "output": str(args.output.resolve())}))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
