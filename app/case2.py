"""Расписание номера по правилам сна и предыдущим настройкам гостя."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_NIGHT = re.compile(r"\b(?:спать|сплю|спит|ночью|на ночь|для сна|при засыпании|засыпа\w*)\b")
_CELSIUS = re.compile(r"(?<!\d)(1[6-9]|2[0-6])(?:[.,](\d+))?\s*(?:°\s*c?|градус\w*)", re.I)
_SLEEP_TEMP = {"cool": 17.5, "normal": 19.5, "warm": 21.5}
_GENTLE = re.compile(r"\b(?:мягк\w* (?:будильник|пробуждение)|будит свет|разбудить светом|просыпа\w* от света)\b")
_NEGATED = re.compile(r"\b(?:нет|не|никогда|без)\b")
_HABIT = re.compile(r"\b(?:люб\w*|предпочита\w*|нрав\w*|выбира\w*|привык\w*|обычно)\b")


@dataclass(frozen=True)
class SleepRoutine:
    temperature: float
    gentle_alarm: bool
    favorite_track: str | None

    def room_values(self):
        sound = (
            {"track_id": self.favorite_track, "volume": 20, "timer_minutes": 45}
            if self.favorite_track else {"volume": 0}
        )
        return {
            "light": {"brightness": 0},
            "curtains": {"position": 0},
            "climate": {"temperature_c": self.temperature},
            "sound": sound,
            "dnd": {"enabled": True},
        }


def _normalized(text):
    return text.casefold().replace("ё", "е")


def resolve_routine(user, notes, tracks=None):
    preferences = user.get("sleep_preferences") or {}
    temperature = _SLEEP_TEMP.get(preferences.get("temperature"), _SLEEP_TEMP["normal"])
    dislikes = _normalized(" ".join(preferences.get("dislikes") or []))
    gentle = bool(re.search(r"резк\w*|громк\w* (?:звук|будильник)", dislikes))
    track_ids = list(preferences.get("favorite_track_ids") or [])
    titles = {
        track["track_id"]: _normalized(track["title"])
        for track in (tracks or ())
        if track.get("track_id") and track.get("title")
    }
    if tracks is not None:
        track_ids = [track for track in track_ids if track in titles]
    blocked_tracks = {
        key for key, title in titles.items()
        if re.search(rf"(?<!\w)(?:{re.escape(title)}|{re.escape(key)})(?!\w)", dislikes)
    }

    for note in notes:
        note = _normalized(re.sub(r"^\[[^]]*]\s*", "", note))
        for sentence in re.split(r"[!?;\n]+|\.(?=\s|$)", note):
            if re.search(r"\b(?:раньше|прежде)\b", sentence) and not re.search(r"\b(?:сейчас|теперь)\b", sentence):
                continue
            if re.search(r"\b(?:не мерзля\w*|не любит тепло|не люблю тепло)\b", sentence):
                temperature = _SLEEP_TEMP["normal"]
            elif re.search(r"\bмерзля\w*\b", sentence):
                temperature = _SLEEP_TEMP["warm"]
            if re.search(r"\b(?:люблю|любит|предпочита\w*) (?:спать в )?прохлад\w*\b", sentence):
                if not re.search(r"\bне (?:люблю|любит|предпочита\w*)\b", sentence):
                    temperature = _SLEEP_TEMP["cool"]
            value = _CELSIUS.search(sentence) if _NIGHT.search(sentence) else None
            if value and not _NEGATED.search(sentence[max(0, value.start() - 15):value.start()]):
                measured = float(".".join(number for number in value.groups() if number is not None))
                if measured < 18.5:
                    temperature = _SLEEP_TEMP["cool"]
                elif measured > 20.5:
                    temperature = _SLEEP_TEMP["warm"]
                else:
                    temperature = _SLEEP_TEMP["normal"]

            if re.search(r"\b(?:не разбуд\w*|не сработал\w*)\b", sentence) and re.search(r"\bсвет\w*\b", sentence):
                gentle = False
            elif (_GENTLE.search(sentence) and not re.search(r"\bне (?:любит|люблю|хочет|хочу)\b", sentence)) or re.search(
                r"\b(?:не любит|не люблю|не использовать|без)\b[^.!?]{0,30}\bрезк\w*\b[^.!?]{0,30}\b(?:будильник\w*|звук\w*)\b",
                sentence,
            ):
                gentle = True

            for track, title in titles.items():
                match = re.search(rf"(?<!\w)(?:{re.escape(title)}|{re.escape(track)})(?!\w)", sentence)
                if not match:
                    continue
                before = sentence[max(0, match.start() - 55):match.start()]
                if re.search(r"\b(?:не (?:люб\w*|нрав\w*|включ\w*|перенос\w*)|раздража\w*|без)\b", before):
                    blocked_tracks.add(track)
                elif _HABIT.search(before) or (_NIGHT.search(sentence) and re.search(r"\b(?:включ\w*|став\w*|слуш\w*)\b", sentence)):
                    blocked_tracks.discard(track)
                    track_ids.insert(0, track)

    favorite = next((track for track in track_ids if track not in blocked_tracks), None)
    return SleepRoutine(temperature, gentle, favorite)


def _bedtime_before(wake, bedtime_time, home):
    local_wake = wake.astimezone(home)
    instant = wake.astimezone(UTC)
    for days_ago in (0, 1):
        date = local_wake.date() - timedelta(days=days_ago)
        local_time = datetime.combine(date, bedtime_time, home)
        # Both folds are possible in the repeated hour. A skipped civil time
        # moves forward by the timezone gap when converted back from UTC.
        candidates = {local_time.replace(fold=fold).astimezone(UTC) for fold in (0, 1)}
        valid = [candidate for candidate in candidates if candidate < instant and
                 candidate.astimezone(home).replace(tzinfo=None) >= local_time.replace(tzinfo=None)]
        if valid:
            return max(valid)
    return instant


def build_scenario(stay, user, notes, tracks=None):
    check_in = datetime.fromisoformat(stay["check_in_at"]).astimezone(UTC)
    wake_local = datetime.fromisoformat(stay["target_wake_at"])
    wake = wake_local.astimezone(UTC)
    bedtime_time = time.fromisoformat(user.get("weekday_bedtime") or "23:00")
    try:
        hotel = ZoneInfo(stay["hotel_timezone"])
    except (KeyError, TypeError, ZoneInfoNotFoundError):
        hotel = wake_local.tzinfo
    try:
        home = ZoneInfo(user["timezone"])
    except (KeyError, TypeError, ZoneInfoNotFoundError):
        home = hotel
    bedtime = _bedtime_before(wake, bedtime_time, home)
    offset_difference = bedtime.astimezone(hotel).utcoffset() - bedtime.astimezone(home).utcoffset()
    if abs(offset_difference) >= timedelta(hours=3):
        bedtime += timedelta(hours=-1 if offset_difference > timedelta(0) else 1)
    bedtime = max(check_in, min(bedtime, wake))
    routine = resolve_routine(user, notes, tracks)
    steps = []

    def add(at, device, values):
        if check_in <= at <= wake:
            steps.append((at, device, values))

    evening = max(check_in, bedtime - timedelta(hours=1))
    if evening < bedtime:
        add(evening, "light", {"brightness": 20, "color_temperature_k": 2700})
    for device, values in routine.room_values().items():
        add(bedtime, device, values)
    add(check_in, "alarm", {
        "at": wake.astimezone(hotel).isoformat(timespec="seconds"),
        "mode": "light" if routine.gentle_alarm else "both",
        "window_minutes": 0,
    })
    for minutes, brightness in ((30, 10), (22, 30), (15, 60)):
        at = wake - timedelta(minutes=minutes)
        if at > bedtime:
            add(at, "light", {"brightness": brightness, "color_temperature_k": 2700})
    dawn = wake - timedelta(minutes=15)
    if dawn > bedtime:
        add(dawn, "curtains", {"position": 50})
    return [
        {"at": at.astimezone(hotel).isoformat(timespec="seconds"), "device_id": device, "values": values}
        for at, device, values in sorted(steps, key=lambda step: step[0])
    ]
