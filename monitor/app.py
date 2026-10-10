"""Low overhead piano practice monitor. Only the Python standard library is used."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"
STATE = ROOT / ".active-session.json"
DATA = ROOT / "data" / "sessions"
INDEX = ROOT / "docs" / "data.json"
LOCAL = ROOT / "monitor" / "index.html"
ZONE = ZoneInfo("Asia/Shanghai")
DEFAULTS = {"input": "auto", "output": "auto", "volume": 70,
            "threshold_db": -38, "idle_seconds": 90, "port": 8765}


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


def devices() -> list[dict]:
    try:
        cards = Path("/proc/asound/cards").read_text()
        pcm = Path("/proc/asound/pcm").read_text()
    except OSError:
        return []
    names = {int(n): (card, title.strip()) for n, card, title in
             re.findall(r"(?m)^\s*(\d+) \[([^]]+)\]: .*? - (.+)$", cards)}
    result = []
    for n, dev, title, modes in re.findall(r"(?m)^(\d+)-(\d+): (.+?) : (.+)$", pcm):
        card_id, card_name = names.get(int(n), (n, title))
        result.append({"id": f"hw:CARD={card_id.strip()},DEV={int(dev)}",
                       "name": card_name, "input": "capture" in modes,
                       "output": "playback" in modes})
    return result


def choose(configured: str, direction: str) -> str | None:
    available = [d for d in devices() if d[direction]]
    if configured != "auto":
        return configured if any(d["id"] == configured for d in available) else None
    for d in available:
        if "USB Audio Device" in d["name"]:
            return d["id"]
    return None


class Monitor:
    def __init__(self):
        self.lock = threading.RLock()
        self.config = DEFAULTS.copy()
        try:
            self.config.update(json.loads(CONFIG.read_text()))
        except (OSError, ValueError):
            pass
        self.audio: subprocess.Popen | None = None
        self.input_id = None
        self.output_id = None
        self.level_db = -100.0
        self.peak_db = -100.0
        self.audio_status = "正在连接音频设备"
        self.active: dict | None = None
        self.git_event = threading.Event()
        try:
            self.active = json.loads(STATE.read_text())
            self.finish(self.active["last_activity"])
        except (OSError, ValueError, KeyError):
            self.active = None
        threading.Thread(target=self.git_loop, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return {"config": self.config.copy(), "devices": devices(),
                    "input_id": self.input_id, "output_id": self.output_id,
                    "level_db": round(self.level_db, 1), "peak_db": round(self.peak_db, 1),
                    "audio_status": self.audio_status, "session": self.active,
                    "server_time": dt.datetime.now(ZONE).isoformat()}

    def update(self, data: dict):
        with self.lock:
            new = self.config.copy()
            for key in ("input", "output"):
                if key in data:
                    value = data[key]
                    valid = {"auto"} | {d["id"] for d in devices() if d[key]}
                    if not isinstance(value, str) or value not in valid:
                        raise ValueError(f"无效的{key}设备")
                    new[key] = value
            for key, minimum, maximum in (("volume", 0, 150),
                                          ("threshold_db", -80, -10),
                                          ("idle_seconds", 5, 3600)):
                if key in data:
                    value = data[key]
                    if isinstance(value, bool) or not isinstance(value, (float, int)) or not minimum <= value <= maximum:
                        raise ValueError(f"{key}超出范围")
                    new[key] = value
            changed_device = (new["input"], new["output"]) != (self.config["input"], self.config["output"])
            volume_changed = new["volume"] != self.config["volume"]
            self.config = new
            atomic_json(CONFIG, new)
            if changed_device and self.audio:
                self.audio.terminate()
            elif volume_changed and self.audio and self.audio.stdin:
                try:
                    self.audio.stdin.write(f"VOLUME {new['volume']}\n")
                    self.audio.stdin.flush()
                except BrokenPipeError:
                    pass

    def process_level(self, rms: float, peak: float):
        now = time.time()
        with self.lock:
            self.level_db = 20 * math.log10(max(rms, 1e-6))
            self.peak_db = 20 * math.log10(max(peak, 1e-6))
            threshold = self.config["threshold_db"]
            if self.level_db >= threshold:
                if self.active is None:
                    self.active = {"start": now, "last_activity": now}
                    atomic_json(STATE, self.active)
                else:
                    self.active["last_activity"] = now
                    if now - self.active.get("saved_at", 0) > 10:
                        self.active["saved_at"] = now
                        atomic_json(STATE, self.active)
            elif self.active and now - self.active["last_activity"] >= self.config["idle_seconds"]:
                self.finish(self.active["last_activity"])

    def finish(self, end: float):
        if not self.active:
            return
        start = self.active["start"]
        self.active = None
        STATE.unlink(missing_ok=True)
        if end <= start:
            return
        start_date = dt.datetime.fromtimestamp(start, ZONE)
        record = {"id": f"{int(start)}-{uuid.uuid4().hex[:8]}",
                  "start": dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat(),
                  "end": dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat(),
                  "duration_seconds": round(end - start, 1)}
        path = DATA / str(start_date.year) / start_date.strftime("%Y-%m-%d") / (record["id"] + ".json")
        atomic_json(path, record)
        records = []
        for file in DATA.glob("*/*/*.json"):
            try:
                records.append(json.loads(file.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        records.sort(key=lambda item: item["start"])
        atomic_json(INDEX, {"timezone": "Asia/Shanghai", "sessions": records})
        self.git_event.set()

    def git_loop(self):
        self.git_event.set()
        while True:
            self.git_event.wait(60)
            self.git_event.clear()
            try:
                subprocess.run(["git", "add", "data/sessions", "docs/data.json"], cwd=ROOT,
                               timeout=20, check=True, stdout=subprocess.DEVNULL)
                diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT,
                                      timeout=20)
                if diff.returncode:
                    subprocess.run(["git", "commit", "-m", "Record piano practice session"],
                                   cwd=ROOT, timeout=20, check=True, stdout=subprocess.DEVNULL)
                subprocess.run(["git", "push", "origin", "main"], cwd=ROOT,
                               timeout=30, check=True, stdout=subprocess.DEVNULL)
            except (subprocess.SubprocessError, OSError) as exc:
                print(f"Git sync delayed: {exc}", flush=True)

    def audio_loop(self):
        while True:
            input_id = choose(self.config["input"], "input")
            output_id = choose(self.config["output"], "output")
            if not input_id or not output_id:
                with self.lock:
                    self.audio_status = "等待所选音频设备连接"
                    self.level_db = self.peak_db = -100
                    if self.active:
                        self.finish(self.active["last_activity"])
                time.sleep(3)
                continue
            try:
                process = subprocess.Popen([str(ROOT / "audio" / "audio"), input_id,
                                            output_id, str(self.config["volume"])],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True, bufsize=1)
                with self.lock:
                    self.audio = process
                    self.input_id, self.output_id = input_id, output_id
                    self.audio_status = "正在启动音频"
                assert process.stdout
                for line in process.stdout:
                    if line.startswith("READY"):
                        with self.lock:
                            self.audio_status = "音频正常"
                    elif line.startswith("LEVEL "):
                        try:
                            _, rms, peak = line.split()
                            self.process_level(float(rms), float(peak))
                        except ValueError:
                            pass
                process.wait()
            except OSError as exc:
                print(f"Audio error: {exc}", flush=True)
            with self.lock:
                self.audio = None
                self.audio_status = "音频已断开，正在重试"
                self.level_db = self.peak_db = -100
                if self.active:
                    self.finish(self.active["last_activity"])
            time.sleep(2)


monitor = Monitor()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            self.send_json(monitor.snapshot())
        elif self.path in ("/", "/index.html"):
            body = LOCAL.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/config":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("请求过大或为空")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("设置格式错误")
            monitor.update(data)
            self.send_json(monitor.snapshot())
        except (ValueError, OSError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def send_json(self, value, code=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    threading.Thread(target=monitor.audio_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", int(monitor.config["port"])), Handler).serve_forever()
