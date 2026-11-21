"""Isolated HTTP simulator for repeatable /api/v1 integration tests; no real API calls."""
import copy
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE = {"light": {"brightness": 50, "color_temperature_k": 4000}, "climate": {"temperature_c": 20},
         "sound": {"track_id": "track_01", "volume": 0, "timer_minutes": 0},
         "curtains": {"position": 100}, "alarm": {"at": None, "mode": "both", "window_minutes": 0},
         "dnd": {"enabled": False}}
DATASET = {"stays": [{"record_id": "stay1", "user_id": "user1", "check_in_at": "2026-11-21T20:00:00+03:00",
                      "target_wake_at": "2026-11-22T07:00:00+03:00", "hotel_timezone": "Europe/Moscow"}],
           "users": [{"user_id": "user1", "weekday_bedtime": "23:00", "timezone": "Europe/Moscow",
                      "sleep_preferences": {"temperature": "normal", "favorite_track_ids": [], "dislikes": []}}],
           "tracks": [], "events": [], "chat_history": []}
LOCK = threading.Lock()
RUNS = set()
SESSIONS = {}
AUDIT = []
CONTROL = {"fail_state": False, "fail_device": None}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlparse(self.path)
        query = parse_qs(route.query)
        with LOCK:
            AUDIT.append({"method": "GET", "path": route.path, "query": query})
            if route.path == "/_audit":
                return self.reply(200, {"requests": AUDIT, "run_count": len(RUNS),
                                        "sessions": {sid: item["run_id"] for sid, item in SESSIONS.items()}})
            if route.path == "/case/2/getData":
                return self.reply(200, {"dataset": DATASET})
            if route.path == "/room/devices":
                if query.get("run_id", [""])[0] not in RUNS:
                    return self.reply(400, {"error": "missing run_id"})
                return self.reply(200, {"devices": [{"device_id": "light"}]})
            if route.path.startswith("/room/") and route.path.endswith("/state"):
                sid = route.path.split("/")[2]
                if CONTROL["fail_state"]:
                    return self.reply(503, {"error": "simulated failure"})
                if sid not in SESSIONS:
                    return self.reply(404, {"error": "unknown session"})
                return self.reply(200, {"state": SESSIONS[sid]["state"]})
        return self.reply(404, {})

    def do_POST(self):
        route = urlparse(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        with LOCK:
            AUDIT.append({"method": "POST", "path": route.path, "body": body})
            if route.path == "/_control":
                CONTROL.update(body)
                return self.reply(200, {"ok": True})
            if route.path == "/runs":
                run_id = str(uuid.uuid4())
                RUNS.add(run_id)
                return self.reply(200, {"run_id": run_id, "dataset_id": "dev",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()})
            if route.path == "/room/session":
                if body.get("run_id") not in RUNS:
                    return self.reply(400, {"error": "unknown run"})
                sid = str(uuid.uuid4())
                SESSIONS[sid] = {"run_id": body["run_id"], "state": copy.deepcopy(STATE)}
                return self.reply(200, {"session_id": sid, "initial_state": copy.deepcopy(STATE)})
            if route.path.startswith("/room/"):
                parts = route.path.split("/")
                sid = parts[2]
                if sid not in SESSIONS:
                    return self.reply(404, {"error": "unknown session"})
                if parts[3] == "scenario":
                    return self.reply(200, {"ok": True})
                if parts[3] == "action":
                    if body["device_id"] == CONTROL["fail_device"]:
                        return self.reply(503, {"error": "simulated failure"})
                    SESSIONS[sid]["state"][body["device_id"]].update(body["values"])
                    return self.reply(200, {"state": SESSIONS[sid]["state"]})
        return self.reply(404, {})


if __name__ == "__main__":
    import os
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("FAKE_SIM_PORT", "18888"))), Handler).serve_forever()
