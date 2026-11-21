"""Расписание номера по правилам сна и предыдущим настройкам гостя."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_NIGHT = re.compile(r"\b(?:спать|сплю|спит|ночью|на ночь|для сна|перед сном|отбо(?:й|я|е|ем|ю)|засыпа\w*)\b")
_CELSIUS = re.compile(r"(?<!\d)(1[6-9]|2[0-6])(?:[.,](\d+))?\s*(?:°\s*c?|градус\w*)", re.I)
_SLEEP_TEMP = {"cool": 17.5, "normal": 19.5, "warm": 21.5}
_GENTLE = re.compile(r"\b(?:мягк\w* (?:будильник|пробуждение)|будит свет|разбудить светом|просыпа\w* от света)\b")
_NEGATED = re.compile(r"\b(?:нет|не|никогда|без)\b")
_HABIT = re.compile(r"\b(?:люб\w*|предпочита\w*|нрав\w*|выбира\w*|привык\w*|обычно)\b")
_NOTE_SOURCE = re.compile(r"^\[\S*\s+(?P<source>[^]]*)]\s*")
_REPORTED = re.compile(
    r"\b(?:по словам|сказал\w*|говорил\w*|сообщил\w*|утвержда\w*|просил\w*|"
    r"хоч\w*|хотел\w*|предпочита\w*|люб(?:лю|ит|ят|им|ите|ил\w*)|нрав\w*)\b"
)
_OBSERVED = re.compile(r"\b(?:фактически|телеметри\w*|показани\w*|измер\w*|замер\w*|зафиксир\w*)\b")
_AUDIO_CONTINUATION = re.compile(r"^(?:громкост\w*|таймер\w*)\b")
_VOLUME = re.compile(r"\bгромкост\w*\s*[:=–—]?\s*(?:на\s+)?(\d{1,3})\s*(?:%|процент\w*\b)")
_TIMER = re.compile(r"\bтаймер\w*\s*[:=–—]?\s*(?:на\s+)?(\d{1,4})\s*мин(?:ут\w*)?\b")


@dataclass(frozen=True)
class SleepRoutine:
    temperature: float
    gentle_alarm: bool
    favorite_track: str | None
    sound_volume: int = 20
    sound_timer_minutes: int = 45

    def room_values(self, *, scenario=False):
        volume = max(1, min(30, self.sound_volume)) if scenario else self.sound_volume
        timer = min(45, self.sound_timer_minutes or 45) if scenario else self.sound_timer_minutes
        sound = (
            {"track_id": self.favorite_track, "volume": volume, "timer_minutes": timer}
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


def _sleep_evidence(notes):
    evidence = []
    for note in notes:
        note = _normalized(note)
        metadata = _NOTE_SOURCE.match(note)
        body = note[metadata.end():] if metadata else note
        source = metadata["source"].strip() if metadata else ""
        audio_context = ""
        for sentence in re.split(r"[!?;\n]+|\.(?=\s|$)", body):
            sentence = sentence.strip()
            if re.search(r"\b(?:раньше|прежде)\b", sentence) and not re.search(r"\b(?:сейчас|теперь)\b", sentence):
                audio_context = ""
                continue
            if _NIGHT.search(sentence):
                audio_context = sentence
            elif not _AUDIO_CONTINUATION.match(sentence):
                audio_context = ""
            setting = f"{audio_context} {sentence}"
            observed = source in {"hotel", "app"} and (
                source == "hotel" or _OBSERVED.search(setting)
            ) and not _REPORTED.search(setting)
            evidence.append((bool(observed), sentence, audio_context))
    # notes_for supplies chronological order. Stable sorting applies observed settings
    # after reported wishes and keeps the newest evidence at each confidence level.
    return sorted(evidence, key=lambda entry: entry[0])


def resolve_routine(user, notes, tracks=None):
    preferences = user.get("sleep_preferences") or {}
    temperature = _SLEEP_TEMP.get(preferences.get("temperature"), _SLEEP_TEMP["normal"])
    dislikes = _normalized(" ".join(preferences.get("dislikes") or []))
    gentle = bool(re.search(r"резк\w*|громк\w* (?:звук|будильник)", dislikes))
    track_ids = list(preferences.get("favorite_track_ids") or [])
    track_names = {
        track["track_id"]: re.compile(
            rf"(?<!\w)(?:{re.escape(_normalized(track['title']))}|{re.escape(_normalized(track['track_id']))})(?!\w)"
        )
        for track in (tracks or ())
        if track.get("track_id") and track.get("title")
    }
    if tracks is not None:
        track_ids = [track for track in track_ids if track in track_names]
    blocked_tracks = {track for track, name in track_names.items() if name.search(dislikes)}
    sound_settings = {}

    for order, (_, sentence, audio_context) in enumerate(_sleep_evidence(notes)):
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

        for track, name in track_names.items():
            match = name.search(sentence)
            if not match:
                continue
            before = sentence[max(0, match.start() - 55):match.start()]
            if re.search(r"\b(?:не (?:люб\w*|нрав\w*|включ\w*|перенос\w*)|раздража\w*|без)\b", before):
                blocked_tracks.add(track)
            elif _HABIT.search(before) or (_NIGHT.search(sentence) and re.search(
                r"\b(?:включ\w*|став\w*|слуш\w*|игра\w*|звуч\w*)\b", sentence
            )):
                blocked_tracks.discard(track)
                track_ids.insert(0, track)

        if audio_context:
            values = {}
            for field, pattern, limit in (("sound_volume", _VOLUME, 100), ("sound_timer_minutes", _TIMER, 1440)):
                match = pattern.search(sentence)
                if match and int(match[1]) <= limit and not _NEGATED.search(sentence[max(0, match.start() - 15):match.start()]):
                    values[field] = (order, int(match[1]))
            if values:
                mentioned = [track for track, name in track_names.items() if name.search(audio_context)]
                if len(mentioned) <= 1:
                    sound_settings.setdefault(next(iter(mentioned), None), {}).update(values)

    favorite = next((track for track in track_ids if track not in blocked_tracks), None)
    chosen_sound = sound_settings.get(None, {}).copy()
    for field, setting in sound_settings.get(favorite, {}).items():
        if field not in chosen_sound or setting[0] > chosen_sound[field][0]:
            chosen_sound[field] = setting
    settings = {field: value for field, (_, value) in chosen_sound.items()}
    return SleepRoutine(temperature, gentle, favorite, **settings)


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
    for device, values in routine.room_values(scenario=True).items():
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
