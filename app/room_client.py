"""Клиент симулятора номера организаторов."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class RoomApiError(Exception):
    pass


def read_json(request, timeout, attempts=1):
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError:
            raise
        except (URLError, TimeoutError, OSError):
            if attempt == attempts - 1:
                raise
            time.sleep(attempt + 1)


class RoomClient:
    def __init__(self, base_url: str, run_token: str, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.run_token = run_token
        self.timeout = timeout

    def _request(self, method: str, path: str, body=None, attempts: int = 1):
        headers = {"Authorization": f"Bearer {self.run_token}"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            return read_json(request, timeout=self.timeout, attempts=attempts)
        except HTTPError as exc:
            raise RoomApiError(f"Симулятор ответил HTTP {exc.code}") from exc

    def open_session(self, record_id: str, kind: str, run_id: str | None = None):
        body = {"record_id": record_id, "kind": kind}
        if run_id is not None:
            body["run_id"] = run_id
        return self._request("POST", "/room/session", body, attempts=3)

    def devices(self, run_id: str | None = None):
        query = "?" + urlencode({"run_id": run_id}) if run_id is not None else ""
        return self._request("GET", "/room/devices" + query)

    def set_scenario(self, session_id: str, steps: list):
        session = quote(session_id, safe="")
        return self._request("POST", f"/room/{session}/scenario", {"steps": steps}, attempts=3)

    def action(self, session_id: str, device_id: str, values: dict, at: str | None = None):
        body = {"device_id": device_id, "values": values}
        if at is not None:
            body["at"] = at
        session = quote(session_id, safe="")
        return self._request("POST", f"/room/{session}/action", body)

    def state(self, session_id: str, at: str | None = None):
        session = quote(session_id, safe="")
        query = "?" + urlencode({"at": at}) if at is not None else ""
        return self._request("GET", f"/room/{session}/state{query}")

    def log(self, session_id: str):
        session = quote(session_id, safe="")
        return self._request("GET", f"/room/{session}/log", attempts=3)
