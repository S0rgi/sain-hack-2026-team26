"""Интерактивный заезд в симуляторе и настройки для следующего визита."""

import json
import os
from datetime import UTC, datetime, timedelta
from time import perf_counter
from urllib.parse import urlencode
from urllib.request import Request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.case2 import build_scenario
from app.room_client import RoomApiError, RoomClient, read_json


HOME_FIELDS = {
    "light": {"brightness", "color_temperature_k"},
    "climate": {"temperature_c"},
    "sound": {"track_id", "volume", "timer_minutes"},
    "alarm": {"at", "mode", "window_minutes"},
}


class DemoConfigurationError(ValueError):
    pass


class DemoTimeError(ValueError):
    pass


def demo_stage():
    stage = os.getenv("HACK_RUN_STAGE", "dev").strip().lower()
    if stage not in {"dev", "final"}:
        raise DemoConfigurationError("HACK_RUN_STAGE должен быть dev или final")
    return stage


def create_demo(notes_for, saved=None, *, record_id=None, stage=None):
    base_url = os.getenv("HACK_API_URL", "").rstrip("/")
    token = os.getenv("HACK_TEAM_TOKEN", "").strip()
    if not base_url or not token:
        raise DemoConfigurationError("Для заезда задайте HACK_API_URL и HACK_TEAM_TOKEN на сервере")
    stage = stage or demo_stage()
    payload = {"case": 2, "stage": "final"} if stage == "final" else {"case": 2}
    request = Request(
        base_url + "/runs",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    # A lost response may already have created the run.
    run_info = read_json(request, timeout=30)
    if run_info["stage"] != stage:
        raise RoomApiError("API вернул прогон для другого этапа")
    if stage == "final":
        token = run_info["run_token"]
        query = ""
        session_params = {}
    else:
        query = "?" + urlencode({"dataset_code": run_info["dataset_id"]})
        session_params = {"run_id": run_info["run_id"]}
    request = Request(base_url + "/case/2/getData" + query, headers={"Authorization": f"Bearer {token}"})
    dataset = read_json(request, timeout=30, attempts=3)["dataset"]
    stay = next((stay for stay in dataset["stays"] if record_id is None or stay["record_id"] == record_id), None)
    if stay is None:
        raise ValueError("Заезд не найден в наборе")
    user = next(user for user in dataset["users"] if user["user_id"] == stay["user_id"])
    tracks = dataset.get("tracks") or []
    track_ids = [track["track_id"] for track in tracks]
    started_at = perf_counter()
    notes = notes_for(dataset, stay["user_id"], as_of=stay["check_in_at"])
    scenario = build_scenario(stay, user, notes, tracks=tracks)
    bedtime = next(
        (step["at"] for step in scenario if step["device_id"] == "light" and step["values"].get("brightness") == 0),
        stay["check_in_at"],
    )
    room = {
        "stage": stage,
        "run_id": run_info["run_id"],
        "at": stay["check_in_at"],
        "check_in": stay["check_in_at"],
        "bedtime": bedtime,
        "wake": stay["target_wake_at"],
        "hotel_timezone": stay.get("hotel_timezone"),
        "expires_at": run_info["expires_at"],
        "scenario": scenario,
        "user": user,
        "notes": notes,
        "track_ids": track_ids,
        "track_catalog": tracks,
        "history": [],
        "changes": {},
    }
    room["saved_changes"] = (
        _apply_preferences(room, saved["changes"])
        if saved and saved["stage"] == stage and saved["user_id"] == stay["user_id"]
        else {}
    )
    room["scenario_ms"] = round((perf_counter() - started_at) * 1000, 2)
    client = RoomClient(base_url, token)
    session = client.open_session(stay["record_id"], "stay", **session_params)
    client.set_scenario(session["session_id"], scenario)
    room.update(client=client, session_id=session["session_id"])
    return room


def is_expired(room):
    return datetime.fromisoformat(room["expires_at"]) <= datetime.now(UTC)


def _hotel_zone(room):
    try:
        return ZoneInfo(room.get("hotel_timezone"))
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return datetime.fromisoformat(room["wake"]).tzinfo


def demo_view(room, at=None):
    selected_at = room["at"]
    if at is not None:
        try:
            selected = datetime.fromisoformat(at)
            check_in = datetime.fromisoformat(room["check_in"])
            wake = datetime.fromisoformat(room["wake"])
            if selected.utcoffset() is not None and check_in <= selected <= wake:
                selected_at = selected.astimezone(_hotel_zone(room)).isoformat()
            else:
                raise ValueError
        except ValueError:
            raise DemoTimeError("Выберите время между заселением и пробуждением с часовым поясом номера") from None
    state = room["client"].state(room["session_id"], selected_at)["state"]
    room["at"] = selected_at
    view = {
        "state": state,
        "scenario": room["scenario"],
        "at": selected_at,
        "check_in": room["check_in"],
        "bedtime": room.get("bedtime", room["check_in"]),
        "wake": room["wake"],
        "hotel_timezone": room.get("hotel_timezone"),
        "expires_at": room["expires_at"],
        "stage": room.get("stage", "dev"),
        "scenario_ms": room.get("scenario_ms", 0),
        "preferences_reused": bool(room.get("saved_changes")),
        "tracks": [{"track_id": track["track_id"], "title": track["title"]} for track in room.get("track_catalog", [])],
    }
    if "checkout" in room:
        view["checkout"] = room["checkout"]
    return view


def remember_actions(room, actions):
    """Запоминает только поля действий, подтверждённых симулятором."""
    changes = room.setdefault("changes", {})
    for action in actions:
        device = action["device_id"]
        allowed = HOME_FIELDS.get(device, ())
        values = {field: value for field, value in action["values"].items() if field in allowed}
        if values:
            changes.setdefault(device, {}).update(values)


def _recipe_steps(room):
    bedtime = datetime.fromisoformat(room["bedtime"])
    for step in room["scenario"]:
        at = datetime.fromisoformat(step["at"])
        device = step["device_id"]
        if device == "light" and at < bedtime:
            yield step
        elif device in ("climate", "sound", "alarm") and at <= bedtime:
            yield step


def checkout_demo(room):
    if "checkout" in room:
        return room["checkout"]
    settings = {}
    for step in _recipe_steps(room):
        settings.setdefault(step["device_id"], {}).update(step["values"])
    for device, values in room["changes"].items():
        settings.setdefault(device, {}).update(values)
    bedtime = datetime.fromisoformat(room["bedtime"])
    dawn = next(
        (step["at"] for step in room["scenario"]
         if datetime.fromisoformat(step["at"]) > bedtime and
         ((step["device_id"] == "light" and step["values"].get("brightness", 0) > 0) or
          (step["device_id"] == "curtains" and step["values"].get("position", 0) > 0))),
        None,
    )
    room["checkout"] = {
        "settings": settings,
        "changed": list(room["changes"]),
        "dawn_at": dawn,
        "wake": room["wake"],
        "stage": room["stage"],
    }
    return room["checkout"]


def saved_preferences(room):
    if not room or not room.get("checkout"):
        return None
    choices = {device: dict(values) for device, values in room["saved_changes"].items()}
    for device, values in room["changes"].items():
        choices.setdefault(device, {}).update(values)
    if not choices:
        return None
    return {
        "stage": room["stage"],
        "user_id": room["user"]["user_id"],
        "changes": choices,
    }


def _next_alarm_at(room, previous):
    zone = _hotel_zone(room)
    wake = datetime.fromisoformat(room["wake"]).astimezone(zone)
    check_in = datetime.fromisoformat(room["check_in"]).astimezone(UTC)
    clock = datetime.fromisoformat(previous).timetz().replace(tzinfo=None)
    for day in (wake.date(), wake.date() - timedelta(days=1)):
        for fold in (0, 1):
            local = datetime.combine(day, clock, zone).replace(fold=fold)
            utc = local.astimezone(UTC)
            if check_in <= utc <= wake.astimezone(UTC) and utc.astimezone(zone).time() == clock:
                return local.isoformat()
    return None


def _replayable_values(device, values):
    values = dict(values)
    if device == "light":
        if "brightness" in values and not 10 <= values["brightness"] <= 30:
            values.pop("brightness")
        if values.get("color_temperature_k", 2700) > 2700:
            values.pop("color_temperature_k")
    elif device == "climate":
        temperature = values.get("temperature_c")
        if temperature is None or not any(low <= temperature <= low + 1 for low in (17, 19, 21)):
            values.pop("temperature_c", None)
    elif device == "sound":
        if values.get("volume", 20) > 30:
            values.pop("volume")
        if not 1 <= values.get("timer_minutes", 45) <= 45:
            values.pop("timer_minutes")
    elif device == "alarm" and not values.get("at"):
        # Acknowledging this morning's alarm must not prevent tomorrow's alarm.
        values.pop("at", None)
    return values


def _apply_preferences(room, changes):
    applied = {}
    for step in _recipe_steps(room):
        device = step["device_id"]
        values = _replayable_values(device, changes.get(device, {}))
        if device == "sound" and values.get("track_id") not in room["track_ids"]:
            values.pop("track_id", None)
        if device == "alarm" and values.get("at") is not None:
            mapped = _next_alarm_at(room, values.pop("at"))
            if mapped is not None:
                values["at"] = mapped
        if values:
            step["values"].update(values)
            applied.setdefault(device, {}).update(values)
    return applied
