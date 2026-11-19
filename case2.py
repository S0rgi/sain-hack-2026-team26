"""Построение сценария и обработка основных просьб гостя."""

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_datetime(value):
    return datetime.fromisoformat(value)


def build_scenario(stay, user, notes):
    check_in = parse_datetime(stay["check_in_at"])
    wake = parse_datetime(stay["target_wake_at"])
    bedtime_time = time.fromisoformat(user.get("weekday_bedtime") or "23:00")
    bedtime = datetime.combine(wake.date(), bedtime_time, wake.tzinfo)
    if bedtime >= wake:
        bedtime -= timedelta(days=1)
    try:
        home = ZoneInfo(user["timezone"])
        hotel = ZoneInfo(stay["hotel_timezone"])
        home_offset = wake.astimezone(home).utcoffset()
        hotel_offset = wake.astimezone(hotel).utcoffset()
        difference = (hotel_offset - home_offset).total_seconds() / 3600
        if abs(difference) >= 3:
            bedtime += timedelta(hours=1 if difference > 0 else -1)
    except (KeyError, ZoneInfoNotFoundError):
        pass
    bedtime = max(check_in, min(bedtime, wake))
    preference = user.get("sleep_preferences") or {}
    notes_text = " ".join(notes).lower()
    temperature = preference.get("temperature", "normal")
    if "мерзля" in notes_text:
        temperature = "warm"
    elif "люблю прохладу" in notes_text or "любит прохладу" in notes_text:
        temperature = "cool"
    temperature_c = {"cool": 17.5, "warm": 21.5}.get(temperature, 19.5)
    dislikes = " ".join(preference.get("dislikes") or []).lower()
    gentle = "резк" in dislikes or ("резк" in notes_text and "будильник" in notes_text)
    tracks = preference.get("favorite_track_ids") or []
    steps = []

    def add(at, device, values):
        if check_in <= at <= wake:
            steps.append({"at": at.isoformat(timespec="seconds"), "device_id": device, "values": values})

    evening = max(check_in, bedtime - timedelta(hours=1))
    if evening < bedtime:
        add(evening, "light", {"brightness": 20, "color_temperature_k": 2700})
    add(bedtime, "light", {"brightness": 0})
    add(bedtime, "curtains", {"position": 0})
    add(bedtime, "climate", {"temperature_c": temperature_c})
    sound = {"volume": 20, "timer_minutes": 45, "track_id": tracks[0]} if tracks else {"volume": 0}
    add(bedtime, "sound", sound)
    add(bedtime, "dnd", {"enabled": True})
    add(check_in, "alarm", {"at": wake.isoformat(timespec="seconds"), "mode": "light" if gentle else "both", "window_minutes": 0})
    sunrise = wake - timedelta(minutes=30)
    if sunrise > bedtime:
        add(sunrise, "curtains", {"position": 100})
    return sorted(steps, key=lambda step: parse_datetime(step["at"]))


def understand(text, state, at):
    """Вернуть действие и ответ для ограниченного набора простых формулировок."""
    text = text.lower().replace("ё", "е").strip()
    if "градус" in text:
        match = re.search(r"(-?\d+(?:[.,]\d+)?)\s*градус", text)
        if match:
            value = float(match.group(1).replace(",", "."))
            if not 16 <= value <= 26:
                return None, "Допустимая температура от 16 до 26 °C."
            return ("climate", {"temperature_c": value}), f"Установил температуру {value:g} °C."
    if "полчаса" in text and ("позже" in text or "раньше" in text):
        alarm = state.get("alarm", {}).get("at")
        if not alarm:
            return None, "Сначала установите будильник."
        delta = 30 if "позже" in text else -30
        target = parse_datetime(alarm) + timedelta(minutes=delta)
        return ("alarm", {"at": target.isoformat(timespec="seconds")}), f"Установил будильник на {target:%H:%M}."
    if any(word in text for word in ("разбуди", "будильник")):
        if "отмен" in text or "выключ" in text:
            return ("alarm", {"at": None}), "Выключил будильник."
        match = re.search(r"\b(?:в|на)\s*(\d{1,2})(?::(\d{2}))?\b", text)
        hour_words = {"семь": 7, "восемь": 8, "шесть": 6, "девять": 9}
        word_match = re.search(r"\b(?:в|на)\s*(семь|восемь|шесть|девять)\b", text)
        if match or word_match:
            hour = int(match.group(1)) if match else hour_words[word_match.group(1)]
            minute = int(match.group(2) or 0) if match else 0
            if hour > 23 or minute > 59:
                return None, "Не получилось распознать время будильника."
            now = parse_datetime(at)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return ("alarm", {"at": target.isoformat(timespec="seconds")}), f"Установил будильник на {target:%H:%M}."
    changes = [("прохладнее", "climate", "temperature_c", -1, 16, 26), ("холоднее", "climate", "temperature_c", -1, 16, 26), ("теплее", "climate", "temperature_c", 1, 16, 26), ("ярче", "light", "brightness", 10, 0, 100), ("темнее", "light", "brightness", -10, 0, 100), ("тише", "sound", "volume", -10, 0, 100)]
    for phrase, device, field, change, lower, upper in changes:
        if phrase in text:
            current = state[device][field]
            value = min(upper, max(lower, current + change))
            if current == value:
                return None, "Достигнут предел настройки устройства."
            return (device, {field: value}), "Изменил настройку номера."
    if "штор" in text:
        if "открой" in text:
            return ("curtains", {"position": 100}), "Открыл шторы."
        if "закрой" in text:
            return ("curtains", {"position": 0}), "Закрыл шторы."
    if "не беспокоить" in text:
        return ("dnd", {"enabled": True}), "Включил режим «Не беспокоить»."
    return None, "Не получилось выполнить эту просьбу."
