"""Сервис команды для обработки набора кейса 2 и показа номера."""

import copy
import json
import logging
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.case2 import build_scenario
from app.llm import LLMClient, LLMError
from app.room_client import RoomApiError, RoomClient, read_json

app = FastAPI(title="Sain team26")
origins = [item.strip() for item in os.getenv("FRONTEND_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if item.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                   allow_headers=["Authorization", "Content-Type", "X-Admin-Key"])
logger = logging.getLogger("uvicorn.error")
room_lock = Lock()
live_room = {}


@app.get("/")
def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


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


ROOM_CONTEXT = re.compile(
    r"сон|сн[ауеоы]|спат|сплю|спит|усн|отбо|подъ[её]м|просып|будиль|разбуд|"
    r"свет|ярк|ламп|цвет|температ|градус|прохлад|холод|мерз|м[её]рз|т[её]пл|жарк|"
    r"зву|шум|громк|тиши|трек|track_|дожд|мелод|музык|штор|климат|кондицион|"
    r"засып|ноч|отел|гостиниц|номер|заезд|как дома|домашн|привычн|нрав|"
    r"не люблю|не люб|не беспоко|рассвет|таймер|пробужд|беруш",
    re.IGNORECASE,
)


def notes_for(dataset, user_id):
    history = [
        item for item in dataset.get("chat_history", [])
        if item.get("user_id") == user_id and isinstance(item.get("text"), str)
    ]
    selected = set()
    for index, item in enumerate(history):
        if ROOM_CONTEXT.search(item["text"]):
            selected.update(range(max(0, index - 1), min(len(history), index + 2)))
    notes = [
        f"[{history[index].get('at', '')} {history[index].get('role', '')}] {history[index]['text']}"
        for index in sorted(selected)
    ]
    for item in dataset.get("events", []):
        if item.get("user_id") != user_id:
            continue
        text = item.get("payload", {}).get("text")
        if isinstance(text, str) and (item.get("source") == "hotel" or ROOM_CONTEXT.search(text)):
            notes.append(f"[{item.get('started_at', '')} {item.get('source', '')}] {text}")
    return notes



def execute_message(client, session_id, state, at, text, llm, history, user, notes, track_ids, action_at=None):
    plan = llm.plan(text, state, at, history, user, notes, track_ids)
    if plan.outcome == "refused":
        replies = {
            "medical": "Я не даю медицинских рекомендаций. Обратитесь к специалисту.",
            "out_of_scope": "Я могу помочь только с настройками номера.",
            "invalid_value": "Запрошенное значение выходит за пределы возможностей устройства.",
            "unsupported": "Не удалось определить, как выполнить эту просьбу.",
        }
        return state, "refused", replies[plan.reason], []
    completed = []
    for action in plan.actions:
        try:
            snapshot = client.action(session_id, action.device_id, action.values, action_at)
            state = snapshot["state"]
            completed.append({"device_id": action.device_id, "values": action.values})
        except (RoomApiError, URLError, TimeoutError, OSError):
            reply = "Часть настроек изменена, но остальные применить не удалось." if completed else "Не получилось изменить настройку номера."
            return state, "refused", reply, completed
    return state, "done", "Готово, настройки номера изменены.", completed


def process_request(client, request, llm, history, user, notes, track_ids):
    session = client.open_session(request["record_id"], "request")
    state = session["initial_state"]
    utterances = request["utterances"]
    combined_text = "\n".join(utterance["text"] for utterance in utterances)
    state, response_class, text, actions = execute_message(
        client, session["session_id"], state, request["at"], combined_text,
        llm, history, user, notes, track_ids,
    )
    replies = [{"role": "agent", "at": request["at"], "text": text, "response_class": response_class}]
    history.extend({"role": "user", "text": utterance["text"]} for utterance in utterances)
    history.append({"role": "agent", "text": text, "actions": actions})
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
    stage = "настройка модели"
    try:
        llm = LLMClient()
        stage = "получение набора"
        dataset = fetch_dataset(base_url, req.dataset_code, authorization)
        client = RoomClient(base_url, authorization[7:].strip())
        users = {user["user_id"]: user for user in dataset["users"]}
        tracks = dataset.get("tracks", [])
        track_ids = [track["track_id"] for track in tracks]
        preferences_by_user = {}
        scenario_users = {}
        histories = {user_id: [] for user_id in users}
        for message in dataset.get("chat_history", []):
            user_id = message.get("user_id")
            if user_id in histories and message.get("role") in ("user", "agent") and isinstance(message.get("text"), str):
                histories[user_id].append({"role": message["role"], "text": message["text"]})
        answers = {"stays": [], "requests": []}
        for stay in dataset["stays"]:
            stage = "построение сценария заезда"
            user = users[stay["user_id"]]
            notes = notes_for(dataset, stay["user_id"])
            stage = "извлечение предпочтений"
            if stay["user_id"] not in preferences_by_user:
                preferences_by_user[stay["user_id"]] = llm.preferences(user, notes, track_ids, tracks)
            preferences = preferences_by_user[stay["user_id"]]
            scenario_user = copy.deepcopy(user)
            sleep = scenario_user["sleep_preferences"] = dict(scenario_user.get("sleep_preferences") or {})
            sleep["temperature"] = preferences.temperature
            sleep["favorite_track_ids"] = preferences.favorite_track_ids
            if preferences.gentle_alarm:
                sleep["dislikes"] = (sleep.get("dislikes") or []) + ["резкий будильник"]
            scenario_users[stay["user_id"]] = scenario_user
            stage = "построение сценария заезда"
            scenario = build_scenario(stay, scenario_user, [])
            stage = "создание сеанса заезда"
            session = client.open_session(stay["record_id"], "stay")
            stage = "загрузка сценария"
            client.set_scenario(session["session_id"], scenario)
            answers["stays"].append({"record_id": stay["record_id"], "session_id": session["session_id"]})
        for request in dataset["requests"]:
            stage = "обработка просьбы"
            user_id = request["user_id"]
            answers["requests"].append(process_request(
                client, request, llm, histories[user_id], scenario_users[user_id],
                [preferences_by_user[user_id].room_memory] if preferences_by_user[user_id].room_memory else [],
                track_ids,
            ))
        if hasattr(llm, "usage"):
            logger.info(
                "LLM provider=%s calls=%d input_tokens=%d output_tokens=%d total_tokens=%d missing_usage=%d",
                llm.provider, llm.usage["calls"], llm.usage["input_tokens"], llm.usage["output_tokens"],
                llm.usage["total_tokens"], llm.usage["missing_usage"],
            )
        return answers
    except (HTTPError, URLError, RoomApiError, LLMError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
        cause = exc.__cause__ if isinstance(exc, (RoomApiError, LLMError)) and exc.__cause__ else exc
        code = f" (HTTP {cause.code})" if isinstance(cause, HTTPError) else ""
        detail = f": {exc}" if isinstance(exc, LLMError) else ""
        logger.error("Ошибка на этапе '%s': %s%s%s", stage, type(exc).__name__, code, detail)
        return JSONResponse(status_code=502, content={"error": "Не удалось обработать набор или связаться с симулятором"})


def create_demo(record_id=None):
    base_url = os.environ.get("HACK_API_URL")
    token = os.environ.get("HACK_TEAM_TOKEN", "").strip()
    if not base_url or not token:
        raise ValueError("Для демонстрации задайте HACK_API_URL и HACK_TEAM_TOKEN на сервере")
    request = Request(
        base_url.rstrip("/") + "/runs", data=json.dumps({"case": 2}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST",
    )
    # Do not retry creating a run: a lost response may still have created it.
    with urlopen(request, timeout=30) as response:
        run_info = json.load(response)
    dataset = fetch_dataset(base_url, run_info["dataset_id"], f"Bearer {token}")
    stay = next((item for item in dataset["stays"] if record_id is None or item["record_id"] == record_id), None)
    if stay is None:
        raise ValueError("Заезд не найден в dev-наборе")
    user = next(user for user in dataset["users"] if user["user_id"] == stay["user_id"])
    notes = notes_for(dataset, stay["user_id"])
    tracks = dataset.get("tracks", [])
    track_ids = [track["track_id"] for track in tracks]
    scenario = build_scenario(stay, user, notes)
    client = RoomClient(base_url, token)
    session = client.open_session(stay["record_id"], "stay", run_info["run_id"])
    client.set_scenario(session["session_id"], scenario)
    return {
        "client": client, "session_id": session["session_id"], "at": stay["check_in_at"],
        "check_in": stay["check_in_at"], "wake": stay["target_wake_at"],
        "expires_at": run_info["expires_at"], "scenario": scenario, "user": user,
        "notes": notes, "track_ids": track_ids, "tracks": tracks, "history": [],
    }


def ensure_demo():
    if not live_room or datetime.fromisoformat(live_room["expires_at"]) <= datetime.now(timezone.utc):
        prepared = create_demo()
        live_room.clear()
        live_room.update(prepared)


def demo_view(at=None):
    ensure_demo()
    if at is not None:
        selected = datetime.fromisoformat(at)
        if selected.tzinfo is None or not (datetime.fromisoformat(live_room["check_in"]) <= selected <= datetime.fromisoformat(live_room["wake"])):
            raise ValueError("Выберите время между заселением и пробуждением с часовым поясом номера")
        live_room["at"] = selected.isoformat(timespec="seconds")
    state = live_room["client"].state(live_room["session_id"], live_room["at"])["state"]
    return {"state": state, "scenario": live_room["scenario"], "at": live_room["at"],
            "check_in": live_room["check_in"], "wake": live_room["wake"], "expires_at": live_room["expires_at"]}


@app.post("/api/demo")
def start_demo():
    if os.getenv("ENABLE_LEGACY_DEMO", "false").lower() != "true":
        return JSONResponse(status_code=404, content={"error": "Демонстрационный интерфейс отключён"})
    with room_lock:
        try:
            return demo_view()
        except ValueError as exc:
            return JSONResponse(status_code=503, content={"error": str(exc)})
        except (HTTPError, RoomApiError, URLError, TimeoutError, OSError, KeyError, IndexError, StopIteration):
            return JSONResponse(status_code=502, content={"error": "Не удалось подготовить демонстрационный номер"})


@app.get("/api/room")
def room(at: str | None = None):
    if os.getenv("ENABLE_LEGACY_DEMO", "false").lower() != "true":
        return JSONResponse(status_code=404, content={"error": "Демонстрационный интерфейс отключён"})
    with room_lock:
        if not live_room:
            return JSONResponse(status_code=503, content={"error": "Сначала подготовьте демонстрационный номер"})
        try:
            return demo_view(at)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        except (HTTPError, RoomApiError, URLError, TimeoutError, OSError, KeyError, IndexError, StopIteration):
            return JSONResponse(status_code=502, content={"error": "Не удалось получить состояние номера"})


@app.post("/api/message")
def message(message: Message):
    if os.getenv("ENABLE_LEGACY_DEMO", "false").lower() != "true":
        return JSONResponse(status_code=404, content={"error": "Демонстрационный интерфейс отключён"})
    with room_lock:
        if not live_room:
            return JSONResponse(status_code=503, content={"error": "Сначала подготовьте демонстрационный номер"})
        try:
            ensure_demo()
            client = live_room["client"]
            session_id = live_room["session_id"]
            at = live_room["at"]
            state = client.state(session_id, at)["state"]
            history = live_room["history"]
            state, _, reply, actions = execute_message(
                client, session_id, state, at, message.text, LLMClient(), history,
                live_room["user"], live_room["notes"], live_room["track_ids"], at,
            )
            history.extend([{"role": "user", "text": message.text}, {"role": "agent", "text": reply, "actions": actions}])
            return {"reply": reply, "state": state}
        except (HTTPError, RoomApiError, LLMError, URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, StopIteration):
            return JSONResponse(status_code=502, content={"error": "Не удалось выполнить просьбу в симуляторе"})


from app.product_api import build_router
app.include_router(build_router(create_demo, execute_message))
