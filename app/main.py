"""Сервис команды для обработки набора кейса 2 и показа номера."""

import copy
import logging
import os
import re
import sqlite3
from pathlib import Path
from datetime import datetime
from threading import Event, Lock, Thread
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import demo
from app.case2 import build_scenario
from app.commands import LIMIT_REPLY, fallback_plan, normalize, plan_command, plan_compound_command
from app.llm import LLMClient, LLMError, configured_provider, validate_plan
from app.product_api import build_router, cleanup_expired
from app.room_client import RoomApiError, RoomClient, read_json
from app.voice import router as voice_router, voice_enabled

app = FastAPI(title="Artemisis")
default_origins = "http://localhost:5173,http://127.0.0.1:5173"
origins = [origin.strip() for origin in os.getenv("FRONTEND_ORIGINS", default_origins).split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                   allow_headers=["Authorization", "Content-Type", "X-Admin-Key"])
static_dir = Path(__file__).parent / "static"
app.mount("/assets", StaticFiles(directory=static_dir), name="assets")
app.include_router(voice_router)
logger = logging.getLogger("uvicorn.error")
room_lock = Lock()
live_room = {}
cleanup_stop = Event()
cleanup_thread = None


def cleanup_loop():
    try:
        interval = max(1, int(os.getenv("SESSION_CLEANUP_INTERVAL_SECONDS", "60")))
    except ValueError:
        interval = 60
    while not cleanup_stop.wait(interval):
        try:
            cleanup_expired()
        except (OSError, ValueError, sqlite3.Error):
            logger.exception("Не удалось очистить истёкшие сеансы")


@app.on_event("startup")
def start_cleanup():
    global cleanup_thread
    cleanup_stop.clear()
    cleanup_thread = Thread(target=cleanup_loop, name="session-cleanup", daemon=True)
    cleanup_thread.start()


@app.on_event("shutdown")
def stop_cleanup():
    cleanup_stop.set()
    if cleanup_thread:
        cleanup_thread.join(timeout=2)


@app.get("/")
def home():
    return FileResponse(static_dir / "index.html")


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


def notes_for(dataset, user_id, as_of=None):
    cutoff = datetime.fromisoformat(as_of) if as_of else None

    def relevant(item, time_key):
        return item.get("user_id") == user_id and (
            cutoff is None or datetime.fromisoformat(item[time_key]) <= cutoff
        )

    history = [
        item for item in dataset.get("chat_history", [])
        if relevant(item, "at") and isinstance(item.get("text"), str)
    ]
    selected = set()
    for index, item in enumerate(history):
        if ROOM_CONTEXT.search(item["text"]):
            selected.update(range(max(0, index - 1), min(len(history), index + 2)))
    notes = [(history[index].get("at", ""), history[index].get("role", ""), history[index]["text"])
             for index in sorted(selected)]
    for item in dataset.get("events", []):
        if not relevant(item, "started_at"):
            continue
        text = item.get("payload", {}).get("text")
        if isinstance(text, str) and (item.get("source") == "hotel" or ROOM_CONTEXT.search(text)):
            notes.append((item.get("started_at", ""), item.get("source", ""), text))
    ordered = sorted(notes, key=lambda item: datetime.fromisoformat(item[0]) if cutoff else item[0])
    return [f"[{at} {source}] {text}" for at, source, text in ordered]


def request_notes(text, notes, track_catalog):
    words = set(re.findall(r"[а-яa-z]{4,}", normalize(text)))
    evidence = sorted(
        enumerate(notes),
        key=lambda item: (sum(word in normalize(item[1]) for word in words), item[0]),
        reverse=True,
    )[:12]
    selected = [note[:450] for _, note in sorted(evidence)]
    if track_catalog:
        selected.append("Каталог звуков: " + ", ".join(f"{track['track_id']} — {track['title']}" for track in track_catalog))
    return selected


def describe_actions(actions, track_catalog):
    labels = {
        "light": {"brightness": ("Яркость света", "%"), "color_temperature_k": ("Цвет света", " К")},
        "climate": {"temperature_c": ("Температура", " °C")},
        "sound": {"volume": ("Громкость", "%"), "timer_minutes": ("Таймер звука", " мин")},
        "curtains": {"position": ("Шторы открыты на", "%")},
        "alarm": {"mode": ("Режим будильника", ""), "window_minutes": ("Окно будильника", " мин")},
    }
    tracks = {track["track_id"]: track["title"] for track in (track_catalog or ())}
    phrases = []
    for action in actions:
        device = action["device_id"]
        for field, value in action["values"].items():
            if device == "alarm" and field == "at":
                phrases.append(f"Будильник на {datetime.fromisoformat(value):%H:%M}." if value else "Будильник выключен.")
            elif device == "dnd" and field == "enabled":
                phrases.append(f"Режим «Не беспокоить» {'включён' if value else 'выключен'}.")
            elif device == "sound" and field == "track_id":
                if title := tracks.get(value):
                    phrases.append(f"Выбран звук «{title}».")
            elif setting := labels.get(device, {}).get(field):
                label, unit = setting
                if field == "mode":
                    value = {"light": "свет", "sound": "звук", "both": "свет и звук"}[value]
                elif isinstance(value, (int, float)):
                    value = f"{value:g}".replace(".", ",")
                phrases.append(f"{label} {value}{unit}.")
    return " ".join(phrases) or "Готово, настройки номера изменены."


def execute_message(client, session_id, state, at, text, llm, history, user, notes, track_ids,
                    action_at=None, track_catalog=None, *, hotel_timezone=None):
    compound = plan_compound_command(text, state, at, history, user, notes, track_catalog, hotel_timezone=hotel_timezone)
    if compound:
        plan, rejected = compound
    else:
        plan = plan_command(text, state, at, history, user, notes, track_catalog, hotel_timezone=hotel_timezone)
        rejected = []
    try:
        if plan is None:
            if llm is None:
                raise LLMError("Модель недоступна")
            model = llm() if isinstance(llm, type) or not hasattr(llm, "plan") else llm
            plan = model.plan(text, state, at, history, user, request_notes(text, notes, track_catalog), track_ids)
        validate_plan(plan, track_ids)
    except LLMError as exc:
        logger.info("Локальный разбор: %s", type(exc).__name__)
        plan = fallback_plan(text, notes, track_catalog)
        if plan is None:
            return state, "refused", "Не удалось разобрать просьбу. Укажи устройство и нужную настройку.", []
        validate_plan(plan, track_ids)

    if plan.outcome == "refused":
        replies = {
            "medical": "С вопросами о лечении стоит обратиться к врачу. Могу приглушить свет, закрыть шторы или включить «Не беспокоить».",
            "out_of_scope": "Я могу помочь только с настройками номера.",
            "invalid_value": "Запрошенное значение выходит за пределы возможностей устройства.",
            "limit_reached": LIMIT_REPLY,
            "unsupported": "Не удалось определить, как выполнить эту просьбу. Уточни настройку устройства.",
        }
        return state, "refused", " ".join(rejected) or replies[plan.reason], []
    completed = []
    for action in plan.actions:
        try:
            snapshot = client.action(session_id, action.device_id, action.values, action_at)
            state = snapshot["state"]
            completed.append({"device_id": action.device_id, "values": action.values})
        except (RoomApiError, URLError, TimeoutError, OSError):
            details = [describe_actions(completed, track_catalog)] if completed else []
            reply = " ".join([*details, "Симулятор не подтвердил остальные настройки." if completed else "Не получилось изменить настройку номера.", *rejected])
            return state, "refused", reply, completed
    reply = " ".join([describe_actions(completed, track_catalog), *rejected])
    return state, "refused" if rejected else "done", reply, completed


def process_request(client, request, llm, history, user, notes, track_ids, tracks=None):
    session = client.open_session(request["record_id"], "request")
    state = session["initial_state"]
    utterances = request["utterances"]
    hotel_timezone = request.get("hotel_timezone")
    replies = []
    groups = [utterances]
    if len(utterances) == 2:
        plans = [plan_command(item["text"], state, at=request["at"], history=history, user=user,
                              notes=notes, tracks=tracks, hotel_timezone=hotel_timezone) for item in utterances]
        if all(plan is not None and plan.outcome == "done" for plan in plans):
            first_devices, second_devices = ({action.device_id for action in plan.actions} for plan in plans)
            if first_devices.isdisjoint(second_devices) and not re.match(r"^(?:нет|лучше|вместо)\b", normalize(utterances[1]["text"])):
                groups = [[item] for item in utterances]
    for group in groups:
        combined_text = "\n".join(utterance["text"] for utterance in group)
        at = group[-1]["at"]
        state, response_class, text, actions = execute_message(
            client, session["session_id"], state, at, combined_text,
            llm, history, user, notes, track_ids, track_catalog=tracks, hotel_timezone=hotel_timezone,
        )
        replies.append({"role": "agent", "at": at, "text": text, "response_class": response_class})
        history.extend({"role": "user", "text": utterance["text"]} for utterance in group)
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
    stage = "получение набора"
    try:
        dataset = fetch_dataset(base_url, req.dataset_code, authorization)
        try:
            llm = LLMClient()
        except LLMError:
            llm = None
        client = RoomClient(base_url, authorization[7:].strip())
        users = {user["user_id"]: user for user in dataset["users"]}
        tracks = dataset.get("tracks", [])
        track_ids = [track["track_id"] for track in tracks]
        histories = {user_id: [] for user_id in users}
        answers = {"stays": [], "requests": []}
        for stay in dataset["stays"]:
            stage = "построение сценария заезда"
            user = users[stay["user_id"]]
            notes = notes_for(dataset, stay["user_id"], as_of=stay["check_in_at"])
            scenario = build_scenario(stay, user, notes, tracks)
            stage = "создание сеанса заезда"
            session = client.open_session(stay["record_id"], "stay")
            stage = "загрузка сценария"
            client.set_scenario(session["session_id"], scenario)
            answers["stays"].append({"record_id": stay["record_id"], "session_id": session["session_id"]})
        results = {}
        for request in sorted(dataset["requests"], key=lambda item: datetime.fromisoformat(item["at"])):
            stage = "обработка просьбы"
            user_id = request["user_id"]
            notes = notes_for(dataset, user_id, as_of=request["at"])
            results[request["record_id"]] = process_request(
                client, request, llm, histories[user_id], users[user_id], notes, track_ids, tracks,
            )
        answers["requests"] = [results[request["record_id"]] for request in dataset["requests"]]
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


def create_product_session(record_id):
    room = demo.create_demo(notes_for, record_id=record_id, stage="dev")
    room.pop("confirmed_state", None)
    return room


app.include_router(build_router(create_product_session, execute_message))


class DemoReset(BaseModel):
    preserve_preferences: bool = False


_SIMULATOR_ERRORS = (HTTPError, RoomApiError, URLError, TimeoutError, OSError, TypeError, ValueError,
                     KeyError, IndexError, StopIteration, LLMError)


def demo_error(exc, message):
    cause = exc.__cause__ if isinstance(exc, RoomApiError) and exc.__cause__ else exc
    upstream = f" (HTTP {cause.code})" if isinstance(cause, HTTPError) else ""
    logger.warning("Demo error: %s%s", type(exc).__name__, upstream)
    return JSONResponse(status_code=502, content={"error": message})


def prepare_stay(saved=None):
    candidate = demo.create_demo(notes_for, saved=saved)
    view = demo.demo_view(candidate, use_confirmed_state=True)
    live_room.clear()
    live_room.update(candidate)
    return view


def legacy_demo_enabled():
    return os.getenv("ENABLE_LEGACY_DEMO", "false").lower() == "true"


def disabled_demo():
    return JSONResponse(status_code=404, content={"error": "Общий номер для демонстрации отключён."})


def stay_error(*, allow_checkout=False):
    if not legacy_demo_enabled():
        return disabled_demo()
    if not live_room:
        return JSONResponse(status_code=503, content={"error": "Сначала заселитесь в номер."})
    if demo.is_expired(live_room):
        return JSONResponse(status_code=409, content={"error": "Время заезда истекло. Начните новый заезд."})
    if not allow_checkout and live_room.get("checkout"):
        return JSONResponse(status_code=409, content={"error": "Заезд завершён. Заселитесь снова, чтобы изменить номер."})
    return None


@app.get("/api/config")
def client_config():
    try:
        return {"stage": demo.demo_stage(), "voice": voice_enabled(), "demo": legacy_demo_enabled()}
    except demo.DemoConfigurationError as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})


@app.post("/api/demo")
def start_demo():
    if not legacy_demo_enabled():
        return disabled_demo()
    with room_lock:
        try:
            if not live_room or demo.is_expired(live_room):
                return prepare_stay()
            return demo.demo_view(live_room)
        except demo.DemoConfigurationError as exc:
            return JSONResponse(status_code=503, content={"error": str(exc)})
        except _SIMULATOR_ERRORS as exc:
            return demo_error(exc, "Не удалось подготовить номер. Попробуйте ещё раз.")


@app.post("/api/demo/reset")
def reset_demo(request: DemoReset):
    if not legacy_demo_enabled():
        return disabled_demo()
    with room_lock:
        try:
            saved = demo.saved_preferences(live_room) if request.preserve_preferences else None
            return prepare_stay(saved)
        except demo.DemoConfigurationError as exc:
            return JSONResponse(status_code=503, content={"error": str(exc)})
        except _SIMULATOR_ERRORS as exc:
            return demo_error(exc, "Не удалось начать новый заезд. Текущий заезд сохранён.")


@app.get("/api/room")
def room(at: str | None = None):
    with room_lock:
        if error := stay_error(allow_checkout=at is None):
            return error
        try:
            return demo.demo_view(live_room, at)
        except demo.DemoTimeError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        except _SIMULATOR_ERRORS as exc:
            return demo_error(exc, "Не удалось получить состояние номера.")


@app.post("/api/checkout")
def finish_demo():
    with room_lock:
        if error := stay_error(allow_checkout=True):
            return error
        return demo.checkout_demo(live_room)


@app.post("/api/message")
def message(message: Message):
    with room_lock:
        if error := stay_error():
            return error
        try:
            client = live_room["client"]
            session_id = live_room["session_id"]
            at = live_room["at"]
            state = live_room.pop("confirmed_state", None)
            if state is None:
                state = client.state(session_id, at)["state"]
            history = live_room["history"]
            llm = None if live_room["stage"] == "final" and configured_provider() != "sain" else LLMClient
            state, response_class, reply, actions = execute_message(
                client, session_id, state, at, message.text, llm, history,
                live_room["user"], live_room["notes"], live_room["track_ids"], at,
                track_catalog=live_room["track_catalog"], hotel_timezone=live_room.get("hotel_timezone"),
            )
            history.extend([{"role": "user", "text": message.text}, {"role": "agent", "text": reply, "actions": actions}])
            demo.remember_actions(live_room, actions)
            if response_class == "done":
                live_room["confirmed_state"] = state
            return {"reply": reply, "state": state, "response_class": response_class, "actions": actions}
        except _SIMULATOR_ERRORS as exc:
            return demo_error(exc, "Не удалось обработать просьбу. Попробуйте ещё раз.")
