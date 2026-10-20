"""Bounded frame monitoring: synthetic demo or one explicitly enabled scope session."""
import base64
from collections import deque
from contextlib import nullcontext
from datetime import datetime, timezone
import math
import threading
import time
import uuid

import numpy as np

from instrument import capture, connect_scope
from devices import capabilities, discover, identify, normalize_resource, visa_manager
from scope_analysis import analyze_channel, preview_data


def synthetic_frame(elapsed):
    t = np.arange(5000) / 1_000_000
    frequency = 5000 + 180 * math.sin(elapsed * .45)
    amplitude = 1 + .15 * math.sin(elapsed * .8)
    sine = .2 + amplitude * np.sin(2 * np.pi * frequency * t + elapsed * .6)
    high_samples = round(300 + 70 * math.sin(elapsed * .5))
    pwm = np.where(np.arange(len(t)) % 1000 < high_samples, 3.3, 0.)
    return t, {"SYNTH_SINE_V": sine, "SYNTH_PWM_V": pwm}, {
        "source_kind": "dynamic_synthetic_demo", "synthetic": True,
        "sample_rate_hz": 1_000_000, "calibration": "not_applicable_synthetic",
        "notice": "软件动态生成的独立帧，不是示波器采样。"}


class LiveMonitor:
    def __init__(self, scope_factory=connect_scope, manager_factory=visa_manager):
        self.scope_factory = scope_factory
        self.manager_factory = manager_factory
        self.devices = {}
        self.lock = threading.RLock()
        self.thread = None
        self.stop_event = threading.Event()
        self.stream_id = None
        self.config = None
        self.state = "idle"
        self.error = None
        self.latest = None
        self.frames = deque(maxlen=5)
        self.received_times = deque(maxlen=20)
        self.sequence = 0

    def device_request(self, data):
        if not isinstance(data, dict) or set(data) - {"resource"}:
            raise ValueError("设备识别只接受 resource 地址，不能传入命令。")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("请先停止监看并等待断开，再扫描或识别设备。")
            result = ({"devices": [identify(data["resource"], self.manager_factory)], "truncated": False,
                       "message": "设备身份已读取。"} if "resource" in data else discover(self.manager_factory))
            if "resource" not in data:
                self.devices.clear()
            for device in result["devices"]:
                self.devices.pop(device["resource"], None)
                self.devices[device["resource"]] = device
            while len(self.devices) > 16:
                self.devices.pop(next(iter(self.devices)))
            return result

    def start(self, config):
        allowed = {"source", "view", "resource", "channel", "interval_ms", "allow_configuration"}
        if not isinstance(config, dict) or set(config) - allowed:
            raise ValueError("实时监看配置包含未知参数。")
        source, view = config.get("source", "demo"), config.get("view", "curve")
        interval, channel = config.get("interval_ms", 500), config.get("channel", 1)
        if source not in ("demo", "instrument", "mso5000") or view not in ("curve", "screen"):
            raise ValueError("请选择有效的数据源和显示方式。")
        if type(interval) is not int or not 200 <= interval <= 5000:
            raise ValueError("目标刷新间隔应为 200–5000 毫秒。")
        if type(channel) is not int or channel not in (1, 2, 3, 4):
            raise ValueError("只能选择模拟通道 1–4。")
        resource = None
        if source == "demo" and view == "screen":
            raise ValueError("合成演示没有真实仪器屏幕，请使用波形曲线。")
        if source != "demo":
            if config.get("allow_configuration") is not True:
                raise ValueError("请先明确允许建立仪器会话和读取；曲线采集可能改变传输配置。")
            resource = normalize_resource(config.get("resource"))
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("监看会话仍在运行或关闭中，请先停止并等待断开。")
            device = self.devices.get(resource)
            if source == "instrument":
                if not device or not device["supported"]:
                    raise ValueError("请先扫描或识别实际设备，并选择已有适配器的型号。")
                if view not in device["views"] or channel not in device["channels"]:
                    raise ValueError("所选视图或通道不在该设备的可用能力内。")
            self.config = {"source": source, "view": view, "resource": resource,
                           "channel": channel, "interval_ms": interval, "device": device}
            self.stream_id = uuid.uuid4().hex
            self.state, self.error, self.latest, self.sequence = "starting", None, None, 0
            self.frames.clear()
            self.received_times.clear()
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self._run, args=(self.config.copy(), self.stop_event), daemon=True)
            self.thread.start()
            return self.status()

    def _publish(self, config, stop, *, t=None, channels=None, metadata=None, image=None):
        received = time.monotonic()
        payload = {"received_at_utc": datetime.now(timezone.utc).isoformat(),
                   "synthetic": config["source"] == "demo", "view": config["view"],
                   "metadata": metadata or {}, "has_waveform": t is not None}
        if t is not None:
            payload["plot"] = preview_data(t, channels)
            payload["channels"] = {name: analyze_channel(t, values) for name, values in channels.items()}
        else:
            if not isinstance(image, bytes) or not 24 <= len(image) <= 4 * 1024 * 1024 or image[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("仪器返回的屏幕内容不是有效范围内的 PNG。")
            width, height = int.from_bytes(image[16:20], "big"), int.from_bytes(image[20:24], "big")
            if not 1 <= width <= 4096 or not 1 <= height <= 4096:
                raise ValueError("仪器屏幕尺寸超出允许范围。")
            payload["image_base64"] = base64.b64encode(image).decode("ascii")
            payload["image_size"] = [width, height]
        with self.lock:
            if stop.is_set():
                return
            self.sequence += 1
            payload["sequence"] = self.sequence
            self.latest = payload
            self.received_times.append(received)
            self.state = "running"
            if t is not None:
                self.frames.append({"stream_id": self.stream_id, "sequence": self.sequence,
                    "t": t.copy(), "channels": {n: y.copy() for n, y in channels.items()},
                    "metadata": {**metadata, "received_at_utc": payload["received_at_utc"],
                                 "live_stream_id": self.stream_id, "live_frame_sequence": self.sequence}})

    def _run(self, config, stop):
        began = time.monotonic()
        try:
            context = (nullcontext(None) if config["source"] == "demo" else
                       self.scope_factory(config["resource"], timeout_ms=2000))
            with context as scope:
                if scope is not None:
                    info = scope.info()
                    actual = capabilities(info.vendor or "", info.model or "")
                    if not actual["supported"]:
                        raise ValueError("当前地址的实际设备未匹配到可用适配器，已停止读取。请重新识别型号。")
                    if config["channel"] not in actual["channels"]:
                        raise ValueError("所选通道不存在于当前识别的设备上。")
                    expected = config.get("device")
                    if expected and (info.model != expected["model"] or info.vendor != expected["vendor"]
                                     or (expected["serial"] and getattr(info, "serial", None) != expected["serial"])):
                        raise ValueError("设备身份已变化，请重新识别后再开始。")
                while not stop.is_set():
                    tick = time.monotonic()
                    if config["source"] == "demo":
                        t, channels, metadata = synthetic_frame(tick - began)
                        self._publish(config, stop, t=t, channels=channels, metadata=metadata)
                    elif config["view"] == "screen":
                        self._publish(config, stop, image=scope.screenshot(), metadata={
                            "instrument": {"vendor": info.vendor, "model": info.model,
                                           "serial": getattr(info, "serial", None), "firmware": info.firmware},
                            "source_kind": "instrument_screen", "synthetic": False,
                            "notice": "仪器屏幕周期截图，不提供触屏控制或图片数值测量。"})
                    else:
                        t, channels, metadata = capture(config["resource"], channel=config["channel"],
                            mode="NORMal", allow_configuration=True, max_points=10000,
                            scope_factory=lambda *args, **kwargs: nullcontext(scope))
                        self._publish(config, stop, t=t, channels=channels, metadata=metadata)
                    stop.wait(max(0., config["interval_ms"] / 1000 - (time.monotonic() - tick)))
        except Exception as exc:
            with self.lock:
                if not stop.is_set():
                    self.state = "error"
                    if isinstance(exc, ValueError):
                        message = str(exc)
                        if "Python >=3.14" in message:
                            message = "硬件监看需用 Python 3.14+ 和 pymso5000==0.2.0 启动服务；当前仍可使用动态演示。"
                        self.error = message
                    else:
                        self.error = f"读取失败（{type(exc).__name__}），请核对连接、依赖和设备状态。"
        finally:
            with self.lock:
                if self.state != "error":
                    self.state = "stopped"

    def status(self, include_frame=False):
        with self.lock:
            running = bool(self.thread and self.thread.is_alive())
            fps = ((len(self.received_times) - 1) / (self.received_times[-1] - self.received_times[0])
                   if len(self.received_times) > 1 else None)
            result = {"stream_id": self.stream_id, "state": self.state, "worker_alive": running,
                "config": self.config, "error": self.error, "sequence": self.sequence,
                "received_fps": fps, "age_ms": (time.monotonic() - self.received_times[-1]) * 1000 if self.received_times else None}
            if include_frame:
                result["frame"] = self.latest
            return result

    def stop(self, stream_id=None):
        with self.lock:
            if stream_id is not None and stream_id != self.stream_id:
                raise ValueError("监看会话已变更，本请求不能停止新的会话。")
            self.stop_event.set()
            if self.thread and self.thread.is_alive():
                self.state = "stopping"
            return self.status()

    def snapshot(self, stream_id, sequence):
        with self.lock:
            if type(sequence) is not int or stream_id != self.stream_id:
                raise ValueError("请选择当前监看会话的完整波形帧。")
            frame = next((f for f in self.frames if f["sequence"] == sequence), None)
            if frame is None:
                raise ValueError("该帧已过期或只有屏幕截图，请停止监看后选择当前波形帧。")
            return {**frame, "t": frame["t"].copy(), "channels": {n: y.copy() for n, y in frame["channels"].items()},
                    "metadata": {**frame["metadata"], "live_frame_frozen": True}}

    def close(self):
        self.stop()
        if self.thread:
            self.thread.join(timeout=3)
