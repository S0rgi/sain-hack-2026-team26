"""Guest API: individual demo sessions, device control and persisted chat history."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from urllib.error import HTTPError, URLError

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import demo
from app.llm import Action, LLMClient, LLMError, Plan, validate_plan
from app.room_client import RoomApiError, RoomClient


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class DeviceAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str
    values: dict[str, int | float | str | bool | None]


class GuestMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000)


class TimeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: str = Field(min_length=1, max_length=64)


class RoomView(BaseModel):
    at: str
    check_in: str
    wake: str
    expires_at: str
    scenario: list[dict[str, Any]]
    state: dict[str, dict[str, Any]]


class SessionCreated(BaseModel):
    access_token: str
    token_type: Literal["bearer"]
    room: RoomView


class CatalogView(BaseModel):
    devices: list[dict[str, Any]]
    tracks: list[dict[str, Any]]


class StateView(BaseModel):
    state: dict[str, dict[str, Any]]


class HistoryItem(BaseModel):
    role: Literal["user", "agent"]
    text: str


class HistoryView(BaseModel):
    messages: list[HistoryItem]


class MessageResult(StateView):
    reply: str
    outcome: Literal["done", "refused"]
    actions: list[dict[str, Any]]


_locks = [Lock() for _ in range(64)]


def session_lock(key):
    return _locks[int(key[:8], 16) % len(_locks)]


def db_connect():
    path = Path(os.getenv("ROOM_DB_PATH", "data/rooms.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return conn


def token_hash(authorization):
    if not authorization or not authorization.startswith("Bearer ") or not authorization[7:].strip():
        raise HTTPException(401, "Требуется токен сеанса")
    return hashlib.sha256(authorization[7:].strip().encode()).hexdigest()


def load_session(key):
    with db_connect() as conn:
        row = conn.execute("SELECT data FROM sessions WHERE token_hash = ?", (key,)).fetchone()
    if not row:
        raise HTTPException(404, "Сеанс не найден")
    data = json.loads(row[0])
    if datetime.fromisoformat(data["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(410, "Срок сеанса истёк. Создайте новый сеанс")
    return data


def save_session(key, data):
    with db_connect() as conn:
        conn.execute("INSERT OR REPLACE INTO sessions (token_hash, data) VALUES (?, ?)",
                     (key, json.dumps(data, ensure_ascii=False)))


def room_client():
    base = os.getenv("HACK_API_URL", "").strip()
    token = os.getenv("HACK_TEAM_TOKEN", "").strip()
    if not base or not token:
        raise HTTPException(503, "Не настроен API симулятора")
    return RoomClient(base, token)


def selected_at(data, at):
    if at is None:
        return data["at"]
    try:
        selected = datetime.fromisoformat(at)
        if selected.utcoffset() is None or not datetime.fromisoformat(data["check_in"]) <= selected <= datetime.fromisoformat(data["wake"]):
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(422, "Укажите время между заселением и подъёмом с часовым поясом")
    return selected.isoformat(timespec="seconds")


def public_info(data):
    return {name: data[name] for name in ("at", "check_in", "wake", "expires_at", "scenario")}


def build_router(create_demo, execute_message):
    router = APIRouter(prefix="/api/v1", tags=["guest"])

    @router.post("/sessions", status_code=201, response_model=SessionCreated)
    def create_session(body: CreateSession, x_admin_key: str | None = Header(default=None)):
        configured = os.getenv("FRONTEND_ADMIN_KEY", "")
        if len(configured) < 16:
            raise HTTPException(503, "Ключ создания сеансов должен содержать не менее 16 символов")
        if not x_admin_key or not hmac.compare_digest(x_admin_key.encode(), configured.encode()):
            raise HTTPException(401, "Неверный ключ создания сеансов")
        try:
            data = create_demo(body.record_id)
            client = data.pop("client")
            snapshot = client.state(data["session_id"], data["at"])["state"]
            token = secrets.token_urlsafe(32)
            save_session(hashlib.sha256(token.encode()).hexdigest(), data)
            return {"access_token": token, "token_type": "bearer", "room": {**public_info(data), "state": snapshot}}
        except demo.DemoConfigurationError as exc:
            raise HTTPException(503, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except (HTTPError, URLError, RoomApiError, TimeoutError, OSError, KeyError, IndexError, StopIteration):
            raise HTTPException(502, "Не удалось создать демонстрационный сеанс")

    @router.get("/room", response_model=RoomView)
    def get_room(at: str | None = None, authorization: str | None = Header(default=None)):
        key = token_hash(authorization)
        with session_lock(key):
            data = load_session(key)
            requested = selected_at(data, at)
            try:
                state = room_client().state(data["session_id"], requested)["state"]
            except (RoomApiError, URLError, TimeoutError, OSError, KeyError):
                raise HTTPException(502, "Не удалось получить состояние номера")
            return {**public_info(data), "at": requested, "state": state}

    @router.get("/catalog", response_model=CatalogView)
    def get_catalog(authorization: str | None = Header(default=None)):
        key = token_hash(authorization)
        with session_lock(key):
            data = load_session(key)
            try:
                catalog = room_client().devices(run_id=data.get("run_id"))
                devices = catalog.get("devices", []) if isinstance(catalog, dict) else catalog
                return {"devices": devices, "tracks": data.get("track_catalog", data.get("tracks", []))}
            except (RoomApiError, URLError, TimeoutError, OSError, KeyError):
                raise HTTPException(502, "Не удалось получить каталог устройств")

    @router.post("/time", response_model=RoomView)
    def set_time(body: TimeSelection, authorization: str | None = Header(default=None)):
        key = token_hash(authorization)
        with session_lock(key):
            data = load_session(key)
            requested = selected_at(data, body.at)
            try:
                state = room_client().state(data["session_id"], requested)["state"]
            except (RoomApiError, URLError, TimeoutError, OSError, KeyError):
                raise HTTPException(502, "Не удалось получить состояние номера")
            data["at"] = requested
            save_session(key, data)
            return {**public_info(data), "state": state}

    @router.post("/actions", response_model=StateView)
    def action(body: DeviceAction, authorization: str | None = Header(default=None)):
        key = token_hash(authorization)
        with session_lock(key):
            data = load_session(key)
            try:
                plan = Plan(outcome="done", reason="", actions=[Action(device_id=body.device_id, values=body.values)])
                validate_plan(plan, data["track_ids"])
            except (LLMError, ValueError):
                raise HTTPException(422, "Недопустимые устройство или значения")
            try:
                snapshot = room_client().action(data["session_id"], body.device_id, plan.actions[0].values, data["at"])
                return {"state": snapshot["state"]}
            except (RoomApiError, URLError, TimeoutError, OSError, KeyError):
                raise HTTPException(502, "Не удалось изменить настройки номера")

    @router.get("/messages", response_model=HistoryView)
    def get_messages(authorization: str | None = Header(default=None)):
        key = token_hash(authorization)
        with session_lock(key):
            data = load_session(key)
            return {"messages": [{k: item[k] for k in ("role", "text") if k in item} for item in data["history"]]}

    @router.post("/messages", response_model=MessageResult)
    def post_message(body: GuestMessage, authorization: str | None = Header(default=None)):
        key = token_hash(authorization)
        with session_lock(key):
            data = load_session(key)
            client = room_client()
            try:
                state = client.state(data["session_id"], data["at"])["state"]
                state, outcome, reply, actions = execute_message(
                    client, data["session_id"], state, data["at"], body.text, LLMClient,
                    data["history"], data["user"], data["notes"], data["track_ids"], data["at"],
                    track_catalog=data.get("track_catalog", data.get("tracks", [])))
            except (LLMError, RoomApiError, URLError, TimeoutError, OSError, ValueError, KeyError):
                raise HTTPException(502, "Не удалось обработать просьбу")
            data["history"].extend([{"role": "user", "text": body.text},
                                    {"role": "agent", "text": reply, "actions": actions}])
            save_session(key, data)
            return {"reply": reply, "outcome": outcome, "actions": actions, "state": state}

    return router
