"""Сервис команды для обработки набора кейса 2 и показа номера."""

import logging
import os
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from fastapi import FastAPI, Header
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from case2 import build_scenario, understand
from room_client import RoomApiError, RoomClient, read_json

app = FastAPI(title="Sain team26")
logger = logging.getLogger("uvicorn.error")
room_lock = Lock()
live_room = {}


@app.get("/")
def home():
    return FileResponse(Path(__file__).with_name("index.html"))


@app.get("/health")
def health():
    return {"status": "ok"}


class RunRequest(BaseModel):
    case: int
    dataset_code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


def fetch_dataset(base_url, dataset_code, authorization):
    query = urlencode({"dataset_code": dataset_code})
    request = Request(f"{base_url.rstrip('/')}/case/2/getData?{query}", headers={"Authorization": authorization})
    return read_json(request, timeout=30, attempts=3)["dataset"]


def notes_for(dataset, user_id):
    texts = [item.get("text", "") for item in dataset.get("chat_history", []) if item.get("user_id") == user_id]
    texts += [item.get("payload", {}).get("text", "") for item in dataset.get("events", []) if item.get("user_id") == user_id]
    return [text for text in texts if isinstance(text, str)]


def process_request(client, request):
    session = client.open_session(request["record_id"], "request")
    state = session["initial_state"]
    replies = []
    for utterance in request["utterances"]:
        action, text = understand(utterance["text"], state, request["at"])
        response_class = "refused"
        if action:
            try:
                snapshot = client.action(session["session_id"], action[0], action[1])
                state = snapshot["state"]
                response_class = "done"
            except (RoomApiError, URLError, TimeoutError, OSError):
                text = "Не получилось изменить настройку номера."
        replies.append({"role": "agent", "at": utterance["at"], "text": text, "response_class": response_class})
    return {"record_id": request["record_id"], "session_id": session["session_id"], "replies": replies}


@app.post("/run")
def run(req: RunRequest, authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer ") or not authorization[7:].strip():
        return JSONResponse(status_code=401, content={"error": "Требуется Bearer-токен"})
    if req.case != 2:
        return JSONResponse(status_code=400, content={"error": "Поддерживается только кейс 2"})
    base_url = os.environ.get("HACK_API_URL")
    if not base_url:
        return JSONResponse(status_code=503, content={"error": "Не задан HACK_API_URL"})
    stage = "получение набора"
    try:
        dataset = fetch_dataset(base_url, req.dataset_code, authorization)
        client = RoomClient(base_url, authorization[7:].strip())
        users = {user["user_id"]: user for user in dataset["users"]}
        answers = {"stays": [], "requests": []}
        prepared_live = None
        for stay in dataset["stays"]:
            stage = "построение сценария заезда"
            user = users[stay["user_id"]]
            scenario = build_scenario(stay, user, notes_for(dataset, stay["user_id"]))
            stage = "создание сеанса заезда"
            session = client.open_session(stay["record_id"], "stay")
            stage = "загрузка сценария"
            client.set_scenario(session["session_id"], scenario)
            answers["stays"].append({"record_id": stay["record_id"], "session_id": session["session_id"]})
            prepared_live = {"client": client, "session_id": session["session_id"], "at": stay["check_in_at"], "scenario": scenario}
        for request in dataset["requests"]:
            stage = "обработка просьбы"
            answers["requests"].append(process_request(client, request))
        with room_lock:
            live_room.clear()
            if prepared_live:
                live_room.update(prepared_live)
        return answers
    except (HTTPError, URLError, RoomApiError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
        cause = exc.__cause__ if isinstance(exc, RoomApiError) else exc
        code = f" (HTTP {cause.code})" if isinstance(cause, HTTPError) else ""
        logger.error("Ошибка на этапе '%s': %s%s", stage, type(exc).__name__, code)
        return JSONResponse(status_code=502, content={"error": "Не удалось обработать набор или связаться с симулятором"})


@app.get("/api/room")
def room():
    with room_lock:
        if not live_room:
            return JSONResponse(status_code=503, content={"error": "Ожидаем заселение"})
        try:
            state = live_room["client"].state(live_room["session_id"], live_room["at"])["state"]
            return {"state": state, "scenario": live_room["scenario"]}
        except (RoomApiError, URLError, TimeoutError, OSError):
            return JSONResponse(status_code=502, content={"error": "Не удалось получить состояние номера"})


@app.post("/api/message")
def message(message: Message):
    with room_lock:
        if not live_room:
            return JSONResponse(status_code=503, content={"error": "Ожидаем заселение"})
        try:
            client = live_room["client"]
            session_id = live_room["session_id"]
            at = live_room["at"]
            state = client.state(session_id, at)["state"]
            action, reply = understand(message.text, state, at)
            if action:
                state = client.action(session_id, action[0], action[1], at)["state"]
            return {"reply": reply, "state": state}
        except (RoomApiError, URLError, TimeoutError, OSError):
            return JSONResponse(status_code=502, content={"error": "Не удалось выполнить просьбу в симуляторе"})
