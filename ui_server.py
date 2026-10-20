"""Local ScopeAgent workbench. Run: python ui_server.py"""
import argparse
import base64
import binascii
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
import zipfile
import webbrowser

from scope_agent import ChatClient, Session, VERSION, model_run, parse_json, write_results
from scope_analysis import preview_data
from live_stream import LiveMonitor

ROOT = Path(__file__).resolve().parent
MAX_BODY = 48 * 1024 * 1024
MAX_UPLOAD = 32 * 1024 * 1024
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/live.js": ("live.js", "text/javascript; charset=utf-8"),
          "/live.css": ("live.css", "text/css; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


def preview(session, start=None, end=None):
    return preview_data(session.t, session.channels, start, end)


class Workbench:
    def __init__(self, storage):
        self.storage = Path(storage)
        self.records = {}
        self.lock = threading.RLock()
        self.demo_id = None
        self.live = LiveMonitor()
        self.model_override = None

    def model_config(self):
        return self.model_override if self.model_override is not None else {
            "base_url": os.getenv("SCOPE_BASE_URL", ""), "model": os.getenv("SCOPE_MODEL", ""),
            "api_key": os.getenv("SCOPE_API_KEY", "")}

    def model_status(self):
        config = self.model_config()
        configured = bool(config.get("base_url") and config.get("model"))
        if configured:
            try:
                ChatClient(config["base_url"], config["model"], config.get("api_key"))
            except ValueError:
                configured = False
                config = {}  # Invalid environment URLs/keys must not be reflected to the browser.
        return {"configured": configured, "base_url": config.get("base_url", ""),
                "model": config.get("model", ""), "has_api_key": bool(config.get("api_key")),
                "source": "session" if self.model_override is not None else "environment"}

    def set_model(self, data):
        if set(data) == {"enabled"} and data["enabled"] is False:
            self.model_override = {}
        else:
            if (set(data) != {"base_url", "model", "api_key"}
                    or not isinstance(data["base_url"], str) or not 1 <= len(data["base_url"]) <= 2048
                    or not isinstance(data["model"], str) or not isinstance(data["api_key"], str)):
                raise ValueError("请填写模型服务地址、模型名称和 API Key（免鉴权服务可留空）。")
            config = {**data, "base_url": data["base_url"].strip(), "model": data["model"].strip()}
            ChatClient(config["base_url"], config["model"], config["api_key"])
            self.model_override = config
        return self.model_status()

    def freeze_frame(self, data):
        if set(data) != {"stream_id", "sequence"}:
            raise ValueError("冻结请求需要当前会话和帧序号。")
        frame = self.live.snapshot(data["stream_id"], data["sequence"])
        self.live.stop(data["stream_id"])
        temp = tempfile.TemporaryDirectory(prefix="record-", dir=self.storage)
        try:
            folder = Path(temp.name)
            with (folder / "live-frame.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["time_s", *frame["channels"]])
                writer.writerows(zip(frame["t"], *frame["channels"].values()))
            (folder / "metadata.json").write_text(json.dumps(frame["metadata"], ensure_ascii=False, allow_nan=False), encoding="utf-8")
            session = Session(folder / "live-frame.csv", metadata=folder / "metadata.json")
            session.source["name"] = f"{'合成演示' if frame['metadata'].get('synthetic') else '示波器'} · 冻结帧 {frame['sequence']}"
            session.source["kind"] = frame["metadata"]["source_kind"]
            return self.register(session, temporary=temp, demo=bool(frame["metadata"].get("synthetic")))
        except Exception:
            temp.cleanup()
            raise

    def get(self, record_id):
        if not isinstance(record_id, str) or record_id not in self.records:
            raise ValueError("记录已释放或服务已重启，请重新导入。")
        return self.records[record_id]

    def register(self, session, *, temporary=None, demo=False):
        while len(self.records) >= 3:
            oldest = next(iter(self.records))
            old = self.records.pop(oldest)
            if old.get("temporary"):
                old["temporary"].cleanup()
            if oldest == self.demo_id:
                self.demo_id = None
        record_id = uuid.uuid4().hex
        self.records[record_id] = {"session": session, "temporary": temporary, "demo": demo,
                                   "outcome": None, "archive": None}
        return self.analyze(record_id, {"question": "检查周期、幅度和采样限制，并提出补测建议。", "mode": "local"})

    def demo(self):
        if self.demo_id in self.records:
            return self.payload(self.demo_id)
        data = self.register(Session(ROOT / "demo/synthetic_waveforms.csv", limits=ROOT / "examples/limits.json",
                                     metadata=ROOT / "examples/metadata.json"), demo=True)
        self.demo_id = data["record_id"]
        return data

    def import_files(self, data):
        if not isinstance(data, dict) or set(data) - {"csv", "reference", "limits", "metadata"} or "csv" not in data:
            raise ValueError("请选择波形 CSV，可选参考记录、限值和采集说明。")
        temp = tempfile.TemporaryDirectory(prefix="record-", dir=self.storage)
        paths, total = {}, 0
        try:
            for key, item in data.items():
                if (not isinstance(item, dict) or set(item) != {"name", "content"}
                        or not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 255
                        or not isinstance(item["content"], str)):
                    raise ValueError("上传文件格式不正确。")
                try:
                    raw = base64.b64decode(item["content"], validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError("文件内容未正确编码，请重新选择文件。") from exc
                total += len(raw)
                if total > MAX_UPLOAD or (key in ("metadata", "limits") and len(raw) > 131072):
                    raise ValueError("上传文件合计不能超过 32 MiB；每份 JSON 不能超过 128 KiB。")
                paths[key] = Path(temp.name) / (key + (".csv" if key in ("csv", "reference") else ".json"))
                paths[key].write_bytes(raw)
            session = Session(paths.pop("csv"), **paths)
            session.source["name"] = data["csv"]["name"].replace("\\", "/").rsplit("/", 1)[-1]
            return self.register(session, temporary=temp)
        except Exception:
            temp.cleanup()
            raise

    def analyze(self, record_id, data):
        record = self.get(record_id)
        session = record["session"]
        question = data.get("question", "分析波形，并提出补测建议。")
        mode = data.get("mode", "local")
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 8000 or mode not in ("local", "llm"):
            raise ValueError("请输入 1–8000 字的分析问题，并选择可用分析方式。")
        if set(data) - {"record_id", "question", "mode", "interval"}:
            raise ValueError("分析请求包含未知参数。")
        client = None
        if mode == "llm":
            config = self.model_config()
            if not config.get("base_url") or not config.get("model"):
                raise ValueError("尚未配置模型服务，请点击「模型设置」，或先使用本地规则分析。")
            client = ChatClient(config["base_url"], config["model"], config.get("api_key"))
        interval = data.get("interval")
        if interval is not None:
            if mode != "local" or not isinstance(interval, dict):
                raise ValueError("选区复查使用本地原始采样计算。")
            # Validate before changing the last successful result.
            old_trace = session.trace
            session.trace = []
            try:
                check = session.call("inspect_interval", interval)
            finally:
                session.trace = old_trace
            if check["status"] != "ok":
                raise ValueError(check["error"])
        session.trace = []
        if interval is not None:
            session.call("analyze_record", {})
            session.call("evaluate_limits", {})
            observation = session.call("inspect_interval", interval)
            session.call("propose_capture", {"goal": question[:1000]})
            outcome = {"version": VERSION, "question": question, "status": "completed", "mode": "local_rules",
                       "narrative_verified": True, "summary": f"已复查 {interval['channel']} 选区的 {observation['result']['analysis']['sample_count']} 个原始点。整段记录的限值结论保持独立，局部测量见选区证据。",
                       "evidence_ids": [e["evidence_id"] for e in session.trace if e["status"] == "ok"]}
        else:
            outcome = model_run(session, question, client) if client else session.local_run(question)
        with tempfile.TemporaryDirectory(prefix="report-", dir=self.storage) as folder:
            target = Path(folder) / "result"
            write_results(session, outcome, target)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for file in sorted(target.iterdir()):
                    archive.writestr(file.name, file.read_bytes())
                if session.metadata.get("live_frame_frozen"):
                    raw = io.StringIO(newline="")
                    writer = csv.writer(raw)
                    writer.writerow(["time_s", *session.channels])
                    writer.writerows(zip(session.t, *session.channels.values()))
                    archive.writestr("live-frame.csv", raw.getvalue().encode("utf-8"))
                    archive.writestr("metadata.json", json.dumps(session.metadata, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            record["archive"] = buffer.getvalue()
        record["outcome"] = outcome
        return self.payload(record_id)

    def payload(self, record_id):
        record = self.get(record_id)
        session = record["session"]
        return {"record_id": record_id, "source": session.source, "demo": record["demo"],
                "channels": session.analysis, "limits": session.execute("evaluate_limits", {}),
                "trace": session.trace, "outcome": record["outcome"],
                "metadata": session.metadata, "has_reference": session.reference is not None,
                "llm_available": self.model_status()["configured"],
                "start_s": float(session.t[0]), "end_s": float(session.t[-1]), "sample_count": len(session.t)}

    def close(self):
        self.live.close()
        for record in self.records.values():
            if record.get("temporary"):
                record["temporary"].cleanup()
        self.records.clear()


class Handler(BaseHTTPRequestHandler):
    server_version = "ScopeAgentUI/0.2"

    def send(self, status, content, content_type="application/json; charset=utf-8", *, filename=None):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(content)

    def allowed(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            self.send(403, {"error": "只允许本机地址访问。"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in hosts}:
            self.send(403, {"error": "拒绝其他网站发起的请求。"})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in STATIC:
            name, mime = STATIC[parsed.path]
            self.send(200, (ROOT / "ui" / name).read_bytes(), mime)
            return
        if parsed.path == "/favicon.ico":
            self.send(204, b"", "image/x-icon")
            return
        try:
            if parsed.path == "/api/health":
                self.send(200, {"app": "ScopeAgent", "version": VERSION})
                return
            if parsed.path == "/api/model":
                self.send(200, self.server.workbench.model_status())
                return
            if parsed.path == "/api/live/frame":
                self.send(200, self.server.workbench.live.status(include_frame=True))
                return
            with self.server.workbench.lock:
                if parsed.path == "/api/demo":
                    self.send(200, self.server.workbench.demo())
                    return
                query = urllib.parse.parse_qs(parsed.query)
                record = self.server.workbench.get(query.get("record_id", [None])[0])
                if parsed.path == "/api/plot":
                    start = float(query["start_s"][0]) if "start_s" in query else None
                    end = float(query["end_s"][0]) if "end_s" in query else None
                    self.send(200, preview(record["session"], start, end))
                elif parsed.path == "/api/report":
                    self.send(200, record["archive"], "application/zip", filename="scope-agent-report.zip")
                else:
                    self.send(404, {"error": "页面不存在。"})
        except (ValueError, OSError) as exc:
            self.send(400, {"error": str(exc)})

    def do_POST(self):
        if not self.allowed():
            return
        try:
            if self.path not in ("/api/import", "/api/analyze", "/api/live/start", "/api/live/stop", "/api/live/snapshot", "/api/devices", "/api/model"):
                self.send(404, {"error": "页面不存在。"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ValueError("请求必须使用 JSON 格式。")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                self.send(413, {"error": "上传内容过大，请将波形分段后导入。"})
                return
            self.connection.settimeout(30)
            data = parse_json(self.rfile.read(size).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("请求内容应为对象。")
            if self.path == "/api/model":
                with self.server.workbench.lock:
                    result = self.server.workbench.set_model(data)
                self.send(200, result)
                return
            if self.path == "/api/devices":
                self.send(200, self.server.workbench.live.device_request(data))
                return
            if self.path == "/api/live/start":
                self.send(200, self.server.workbench.live.start(data))
                return
            if self.path == "/api/live/stop":
                if set(data) != {"stream_id"} or not isinstance(data["stream_id"], str):
                    raise ValueError("停止请求需要会话标识。")
                self.send(200, self.server.workbench.live.stop(data["stream_id"]))
                return
            with self.server.workbench.lock:
                if self.path == "/api/live/snapshot":
                    result = self.server.workbench.freeze_frame(data)
                else:
                    result = (self.server.workbench.import_files(data) if self.path == "/api/import" else
                              self.server.workbench.analyze(data.get("record_id"), data))
            self.send(200, result)
        except (ValueError, OSError, UnicodeError) as exc:
            self.send(400, {"error": str(exc)})

    def log_message(self, format, *args):
        pass  # Do not persist source names, user questions or model configuration.


def make_server(storage, port=8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.workbench = Workbench(storage)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true", help="Open the workbench; reuse an existing ScopeAgent service if present")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    if args.open_browser:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
                ready = json.loads(response.read(2048)).get("app") == "ScopeAgent"
            if ready:
                webbrowser.open(url)
                print(f"ScopeAgent UI already running: {url}", flush=True)
                return
        except (OSError, ValueError, AttributeError):
            pass
    (ROOT / ".ui-data").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="session-", dir=ROOT / ".ui-data") as storage:
        try:
            server = make_server(storage, args.port)
        except OSError as exc:
            parser.exit(2, f"Cannot start ScopeAgent on port {args.port}: {exc}\n")
        print(f"ScopeAgent UI: http://127.0.0.1:{server.server_port}", flush=True)
        if args.open_browser:
            webbrowser.open(f"http://127.0.0.1:{server.server_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            server.workbench.close()


if __name__ == "__main__":
    main()
