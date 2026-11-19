"""Построение сценария и обработка основных просьб гостя."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_datetime(value):
    return datetime.fromisoformat(value)


def build_scenario(stay, user, notes):
    check_in = parse_datetime(stay["check_in_at"])
    wake = parse_datetime(stay["target_wake_at"])
    bedtime_time = time.fromisoformat(user.get("weekday_bedtime") or "23:00")
    try:
        home = ZoneInfo(user["timezone"])
        hotel = ZoneInfo(stay["hotel_timezone"])
        home_wake = wake.astimezone(home)
        bedtime = datetime.combine(home_wake.date(), bedtime_time, home)
        if bedtime >= home_wake:
            bedtime -= timedelta(days=1)
        bedtime = bedtime.astimezone(hotel)
        home_offset = bedtime.astimezone(home).utcoffset()
        hotel_offset = bedtime.utcoffset()
        difference = (hotel_offset - home_offset).total_seconds() / 3600
        if abs(difference) >= 3:
            bedtime += timedelta(hours=-1 if difference > 0 else 1)
    except (KeyError, TypeError, ZoneInfoNotFoundError):
        bedtime = datetime.combine(wake.date(), bedtime_time, wake.tzinfo)
        if bedtime >= wake:
            bedtime -= timedelta(days=1)
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
