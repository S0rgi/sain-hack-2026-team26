"""Детерминированные команды для однозначных просьб о настройках номера."""

import re
from datetime import datetime, time, timedelta

from app.case2 import resolve_routine
from app.llm import Action, Plan


_NUMBER = r"\d{1,5}(?:[.,]\d{1,3})?"
_MEDICAL = re.compile(
    r"\b(?:лекарств\w*|таблетк\w*|дозировк\w*|антибиотик\w*|мелатонин\w*|снотворн\w*|"
    r"диагноз\w*|лечить|вылечить|назначь|апноэ|можно ли принимать)\b|"
    r"\b(?:что|можно ли)\s+(?:выпить|принять|пить)\b[^.!?]{0,60}\b(?:уснуть|заснуть|для сна)\b"
)
_OUTSIDE_ROOM = re.compile(
    r"(?:закажи|вызови|забронируй) (?:(?:мне|нам) )?(?:такси|билет\w*|доставку|столик)(?: .*)?"
)
_HOURS = {
    "один": 1, "час": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
    "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
    "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "двадцать один": 21, "двадцать два": 22, "двадцать три": 23,
}
_MINUTES = {"пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40, "сорок пять": 45}
_ORDINAL_HOURS = {"пятого": 5, "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9, "десятого": 10}
_RELATIVE = {
    "ярче": ("light", "brightness", 15, 0, 100),
    "светлее": ("light", "brightness", 15, 0, 100),
    "темнее": ("light", "brightness", -15, 0, 100),
    "тусклее": ("light", "brightness", -15, 0, 100),
    "теплее": ("climate", "temperature_c", 1.0, 16, 26),
    "потеплее": ("climate", "temperature_c", 0.5, 16, 26),
    "прохладнее": ("climate", "temperature_c", -1.0, 16, 26),
    "попрохладнее": ("climate", "temperature_c", -0.5, 16, 26),
    "холоднее": ("climate", "temperature_c", -1.0, 16, 26),
    "потише": ("sound", "volume", -10, 0, 100),
    "тише": ("sound", "volume", -10, 0, 100),
    "громче": ("sound", "volume", 10, 0, 100),
}
_NUMBERED = (
    (r"(?:(?:сделай|поставь|установи|выставь) )?(?:яркость(?: света)?|свет) (?:на |до |в )?(-?" + _NUMBER + r")\s*(?:%|процент\w*)?", "light", "brightness", 0, 100, True, "Яркость света", "%"),
    (r"(?:(?:поставь|установи|выставь|сделай) )?(?:(?:температуру(?: в номере)?|климат) (?:на |до |в )?)?(-?" + _NUMBER + r")\s*(?:°\s*[cс]?|градус\w*)", "climate", "temperature_c", 16, 26, False, "Температура", " °C"),
    (r"(?:(?:поставь|установи|выставь|сделай) )?громкость (?:на |до |в )?(-?" + _NUMBER + r")\s*(?:%|процент\w*)?", "sound", "volume", 0, 100, True, "Громкость", "%"),
)


def normalize(text):
    return " ".join(text.casefold().replace("ё", "е").split())


def done(**changes):
    return Plan(
        outcome="done", reason="",
        actions=[Action(device_id=device, values=values) for device, values in changes.items()],
    )


def refuse(reason):
    return Plan(outcome="refused", reason=reason, actions=[])


def _plain(text):
    text = normalize(text).strip(" .!?,«»\"")
    text = re.sub(r"^(?:(?:пожалуйста|можно|номер|эй|давай)[ ,]+)+", "", text)
    return re.sub(r"[, ]+(?:пожалуйста|спасибо)$", "", text).strip(" .!?,")


def _number(text):
    return float(text.replace(",", "."))


def _setting(device, field, raw, low, high, integral=False):
    value = _number(raw)
    if value < low or value > high or integral and not value.is_integer():
        return refuse("invalid_value")
    return done(**{device: {field: int(value) if integral else value}})


def _explicit_setting(command):
    for pattern, *setting in _NUMBERED:
        if match := re.fullmatch(pattern, command):
            return match[1], setting
    return None


def plan_compound_command(text, state, at, history, user, notes, tracks):
    if "\n" in text or _MEDICAL.search(normalize(text)):
        return None
    segments = re.split(r"(?:,\s*|\s+)(?:и|а также)\s+|\s*;\s*", normalize(text))
    if not 2 <= len(segments) <= 4:
        return None
    changes, rejected, targets = {}, [], []
    for segment in segments:
        plan = plan_command(segment, state, at, history, user, notes, tracks)
        if plan is None:
            return None
        if plan.outcome == "done" and len(plan.actions) == 1 and len(plan.actions[0].values) == 1:
            action = plan.actions[0]
            device = action.device_id
            field = next(iter(action.values))
            changes.setdefault(device, {}).update(action.values)
        elif plan.reason == "invalid_value" and (explicit := _explicit_setting(_plain(segment))):
            _, (device, field, low, high, _, label, unit) = explicit
            rejected.append(f"{label} может быть от {low} до {high}{unit}.")
        else:
            return None
        targets.append((device, field))
    if len(set(targets)) != len(targets):
        return (refuse("invalid_value"), []) if rejected else None
    return (done(**changes) if changes else refuse("invalid_value")), rejected


def _relative_action(command, state, amount=None, mild=False):
    device, field, delta, low, high = _RELATIVE[command]
    current = state[device][field]
    step = abs(delta) if amount is None else _number(amount)
    if amount is None and mild:
        step = {"light": 10, "climate": 0.5, "sound": 5}[device]
    target = current + (step if delta > 0 else -step)
    if amount is None:
        target = min(high, max(low, target))
    return _setting(device, field, str(round(target, 3)), low, high, device != "climate")


def _relative_match(command):
    return re.fullmatch(
        r"(?:(?:сделай|сделайте|давай|в номере|здесь) )?(?:(?:свет|звук) )?"
        r"(?:(чуть(?:-чуть)?|немного|еще(?: немного)?) )?"
        r"(" + "|".join(_RELATIVE) + r")"
        r"(?: на (" + _NUMBER + r")\s*(?:%|процент\w*|°(?:\s*[cс])?|градус\w*)?)?",
        command,
    )


def _followup_direction(history):
    for i in range(len(history) - 1, 0, -1):
        answer = history[i]
        if answer.get("role") != "agent" or "actions" not in answer:
            continue
        actions = answer["actions"]
        if len(actions) != 1:
            return None
        previous = history[i - 1]
        match = _relative_match(_plain(previous.get("text", ""))) if previous.get("role") == "user" else None
        if match:
            command = match[2]
            device, field = _RELATIVE[command][:2]
            if actions[0]["device_id"] == device and field in actions[0]["values"]:
                return command
        return None
    return None


def _previous_alarm(state, history):
    current = state["alarm"]["at"]
    if current:
        return datetime.fromisoformat(current)
    for message in reversed(history):
        for action in reversed(message.get("actions", [])):
            if action["device_id"] == "alarm" and "at" in action["values"]:
                value = action["values"]["at"]
                return datetime.fromisoformat(value) if value else None
    return None


def _alarm(command, state, at, history):
    if re.fullmatch(r"(?:отмени|выключи|отключи|убери|сними|удали) (?:мой |утренний )?будильник", command):
        return done(alarm={"at": None})
    approximate = re.fullmatch(r"(?:разбуди|буди)(?: меня)? (попозже|пораньше|позже|раньше)", command)
    if approximate:
        previous = _previous_alarm(state, history)
        if previous is None:
            return refuse("unsupported")
        minutes = 15 if approximate[1] in {"попозже", "позже"} else -15
        value = previous + timedelta(minutes=minutes)
        return done(alarm={"at": value.isoformat(timespec="seconds")})

    shift = re.fullmatch(
        r"(?:(?:сдвинь|перенеси|передвинь|переставь) (?:будильник )?)?(?:еще )?"
        r"на (?:(\d{1,3}) ?(минут\w*|час\w*)|(полчаса|час|четверть часа|пятнадцать минут)) (раньше|позже)",
        command,
    )
    if shift:
        previous = _previous_alarm(state, history)
        if previous is None:
            return refuse("unsupported")
        number, unit, words, direction = shift.groups()
        minutes = (int(number) * (60 if unit.startswith("час") else 1)) if number is not None else (
            {"полчаса": 30, "час": 60, "четверть часа": 15, "пятнадцать минут": 15}[words]
        )
        value = previous + timedelta(minutes=minutes if direction == "позже" else -minutes)
        return done(alarm={"at": value.isoformat(timespec="seconds")})

    absolute = re.fullmatch(
        r"(?:разбуди(?: меня)?|буди(?: меня)?|(?:поставь|установи|перенеси) (?:мне )?будильник)"
        r"(?: на)? (?:(завтра|сегодня) )?(?:утром )?(?:(?:в|на) )?(.+)",
        command,
    )
    if not absolute:
        return None
    day, clock = absolute.groups()
    hour, minute = _clock_components(clock)
    if hour is None:
        return None
    if hour > 23 or minute > 59:
        return refuse("invalid_value")
    instant = datetime.fromisoformat(at)
    date = instant.date() + timedelta(days=day == "завтра")
    value = datetime.combine(date, time(hour, minute), instant.tzinfo)
    if day is None and value <= instant:
        value += timedelta(days=1)
    return done(alarm={"at": value.isoformat(timespec="seconds")})


def _clock_components(clock):
    suffix = re.search(r" (утра|вечера|ночи|дня)$", clock)
    if suffix:
        clock = clock[:suffix.start()]
    digital = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2})|\s+(\d{2}))?", clock)
    quarter = re.fullmatch(r"без четверти (.+)", clock)
    half = re.fullmatch(r"(?:половине |половина |пол)(\S+)", clock)
    if digital:
        hour, minute = int(digital[1]), int(digital[2] or digital[3] or 0)
    elif quarter and quarter[1] in _HOURS:
        hour, minute = _HOURS[quarter[1]] - 1, 45
    elif half and half[1] in _ORDINAL_HOURS:
        hour, minute = _ORDINAL_HOURS[half[1]] - 1, 30
    elif clock in _HOURS:
        hour, minute = _HOURS[clock], 0
    else:
        times = [
            (_HOURS[clock.removesuffix(" " + name)], value)
            for name, value in _MINUTES.items()
            if clock.endswith(" " + name) and clock.removesuffix(" " + name) in _HOURS
        ]
        if not times:
            return None, None
        hour, minute = times[-1]
    if suffix and suffix[1] in {"дня", "вечера"} and hour < 12:
        hour += 12
    if suffix and suffix[1] == "ночи" and hour == 12:
        hour = 0
    return hour, minute


def _combined_turns(text, state, at, history, user, notes, tracks):
    first, second = text.split("\n", maxsplit=1)
    first_plan = plan_command(first, state, at, history, user, notes, tracks)
    if first_plan is None or first_plan.outcome != "done":
        return None
    preview = {device: values.copy() for device, values in state.items()}
    for action in first_plan.actions:
        preview[action.device_id].update(action.values)
    second = re.sub(r"^(?:нет[, ]+|лучше +)+", "", _plain(second))
    second_plan = plan_command(second, preview, at, history, user, notes, tracks)
    if second_plan is None or second_plan.outcome != "done":
        return second_plan
    first_devices = {action.device_id for action in first_plan.actions}
    if {action.device_id for action in second_plan.actions}.isdisjoint(first_devices):
        return None
    combined = {}
    for action in first_plan.actions + second_plan.actions:
        combined.setdefault(action.device_id, {}).update(action.values)
    return done(**combined)


def _settings_in_notes(notes, context, tracks):
    values = {}
    fields = (
        (r"(?:температур\w*|климат)\D{0,14}(" + _NUMBER + r")\s*(?:°|градус|$)", "climate", "temperature_c", 16, 26, False),
        (r"(?:яркост\w*|свет)\D{0,14}(" + _NUMBER + r")\s*%", "light", "brightness", 0, 100, True),
        (r"(?:цвет\w* (?:света|температур\w*)|свет)\D{0,14}(" + _NUMBER + r")\s*[kк]\b", "light", "color_temperature_k", 2200, 6500, True),
        (r"(?:громкост\w*|звук)\D{0,14}(" + _NUMBER + r")\s*%", "sound", "volume", 0, 100, True),
        (r"таймер\w*\D{0,14}(" + _NUMBER + r")\s*мин", "sound", "timer_minutes", 0, 1440, True),
        (r"штор\w*\D{0,14}(" + _NUMBER + r")\s*%", "curtains", "position", 0, 100, True),
    )
    for note in notes:
        note = normalize(re.sub(r"^\[[^]]*]\s*", "", note))
        if not re.search(context, note) or re.search(r"\b(?:отказ\w*|не хочу|не любит|не люблю)\b", note):
            continue
        for pattern, device, field, low, high, integral in fields:
            if match := re.search(pattern, note):
                number = _number(match[1])
                if low <= number <= high and (not integral or number.is_integer()):
                    values.setdefault(device, {})[field] = int(number) if integral else number
        if re.search(r"\bшторы (?:полностью )?закрыт\w*\b", note):
            values.setdefault("curtains", {})["position"] = 0
        if re.search(r"\bшторы (?:полностью )?открыт\w*\b(?!\s*(?:на|до)\b)", note):
            values.setdefault("curtains", {})["position"] = 100
        if re.search(r"\bсвет (?:полностью )?(?:погашен|выключен)\b", note):
            values.setdefault("light", {})["brightness"] = 0
        for track in tracks or ():
            title = normalize(track["title"])
            if re.search(rf"(?<!\w)(?:{re.escape(title)}|{re.escape(track['track_id'])})(?!\w)", note):
                values.setdefault("sound", {})["track_id"] = track["track_id"]
                break
    return values


def fallback_plan(text, notes, tracks):
    command = _plain(text)
    if re.fullmatch(r"(?:(?:сделай|настрой) )?как дома", command):
        changes = _settings_in_notes(notes, r"\b(?:дома|домашн\w*)\b", tracks)
    elif re.fullmatch(r"(?:(?:сделай|настрой) )?как вчера", command):
        changes = _settings_in_notes(notes, r"\bвчера\b", tracks)
    else:
        return None
    return done(**changes) if changes else None


def plan_command(text, state, at, history, user, notes, tracks):
    """Возвращает None, если просьба требует интерпретации или контекста."""
    if _MEDICAL.search(normalize(text)):
        return refuse("medical")
    if text.count("\n") == 1:
        return _combined_turns(text, state, at, history, user, notes, tracks)
    if "\n" in text:
        return None
    command = _plain(text)
    if _OUTSIDE_ROOM.fullmatch(command):
        return refuse("out_of_scope")
    if re.fullmatch(r"(?:выключи|отключи|погаси) вс[её](?: в номере)?", command):
        return done(light={"brightness": 0}, sound={"volume": 0})

    if explicit := _explicit_setting(command):
        raw, (device, field, low, high, integral, _, _) = explicit
        return _setting(device, field, raw, low, high, integral)

    if re.fullmatch(r"(?:открой|раздвинь|закрой|задвинь) (?:пожалуйста )?(?:шторы|занавески)(?: полностью)?", command):
        return done(curtains={"position": 100 if command.startswith(("открой", "раздвинь")) else 0})
    if re.fullmatch(r"приоткрой (?:пожалуйста )?(?:шторы|занавески)", command):
        return done(curtains={"position": max(25, state["curtains"]["position"])})
    curtain = re.fullmatch(
        r"(открой|приоткрой|раздвинь|закрой|задвинь) (?:шторы|занавески) (?:на |до )("
        + _NUMBER + r"|половину)\s*(?:%|процент\w*)?",
        command,
    )
    if curtain and not curtain[1].startswith(("закрой", "задвинь")):
        target = "50" if curtain[2] == "половину" else curtain[2]
        return _setting("curtains", "position", target, 0, 100, True)
    if re.fullmatch(r"(?:выключи|погаси|отключи) свет", command):
        return done(light={"brightness": 0})
    if re.fullmatch(r"приглуши свет", command):
        return _relative_action("темнее", state)
    if re.fullmatch(r"(?:включи|зажги) свет", command):
        return done(light={"brightness": state["light"]["brightness"] or 50})
    if re.fullmatch(r"(?:выключи|отключи|убери|останови) (?:звук|музыку)", command):
        return done(sound={"volume": 0})
    if re.fullmatch(r"(?:включи |отключи |выключи |сними )?(?:режим )?[«\"]?не беспокоить[»\"]?", command):
        return done(dnd={"enabled": not command.startswith(("отключи", "выключи", "сними"))})
    if re.fullmatch(r"(?:я )?(?:ложусь(?: спать)?|иду спать)", command):
        return done(**resolve_routine(user, notes, tracks).room_values())
    if re.fullmatch(r"(?:(?:я|уже) )?(?:проснул(?:ся|ась)|встал(?:а)?)", command):
        changes = _settings_in_notes(notes, r"\b(?:утром|подъем\w*|просыпа\w*|пробужден\w*)\b", tracks)
        changes = {device: values for device, values in changes.items() if device in ("light", "curtains")}
        changes.setdefault("light", {}).setdefault("brightness", 50)
        return done(**changes, dnd={"enabled": False}, alarm={"at": None})

    alarm = _alarm(command, state, at, history)
    if alarm:
        return alarm
    if match := _relative_match(command):
        return _relative_action(match[2], state, match[3], mild=bool(match[1]))
    if re.fullmatch(r"еще(?: чуть-чуть| немного| чуть)?", command):
        if direction := _followup_direction(history):
            return _relative_action(direction, state, mild=True)
    if command in {"жарковато", "мне жарковато", "здесь жарковато", "в номере жарковато"}:
        return _relative_action("прохладнее", state, mild=True)
    if command in {"мне жарко", "здесь жарко", "в номере жарко"}:
        return _relative_action("прохладнее", state)
    if command in {"мне холодно", "здесь холодно", "в номере холодно", "я мерзну"}:
        return _relative_action("теплее", state)

    sound = re.fullmatch(r"(?:включи|поставь|запусти) (?:мой )?(?:звук|шум|музыку|трек)(?: (.+))?", command)
    if sound:
        catalog = tracks or []
        named = sound[1]
        if named and named not in {"для сна", "для засыпания", "любимый"}:
            track = next((item["track_id"] for item in catalog if named in
                          (normalize(item["title"]), item["track_id"])), None)
            if track is None:
                return None
        else:
            track = resolve_routine(user, notes, tracks).favorite_track or state["sound"]["track_id"]
        if track:
            return done(sound={"track_id": track, "volume": max(20, state["sound"]["volume"])})
    return None
