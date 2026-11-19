"""Сравнение dev-просьб с открытым эталоном организаторов."""

import copy
from collections import defaultdict
from datetime import datetime, timedelta


def matches_expectation(expectation, initial, final):
    device = expectation["device_id"]
    field = expectation["field_id"]
    before = initial[device][field]
    after = final[device][field]
    comparison = expectation["comparison"]
    if comparison == "unchanged":
        return after == before
    if comparison == "increase":
        return after > before
    if comparison == "decrease":
        return after < before
    expected = expectation["value"]
    if comparison == "exact":
        if isinstance(expected, bool):
            return type(after) is bool and after == expected
        return after == expected
    if comparison == "within":
        if expectation["unit"] == "minutes":
            if after is None:
                return False
            actual_time = datetime.fromisoformat(after)
            expected_time = datetime.fromisoformat(expected)
            return abs((actual_time - expected_time).total_seconds()) <= expectation["tolerance"] * 60
        return abs(after - expected) <= expectation["tolerance"]
    raise ValueError(f"Неизвестное сравнение: {comparison}")


def compare_request(request, reference, response, final_state):
    failures = []
    if [reply["response_class"] for reply in response["replies"]] != reference["expected_reply_classes"]:
        failures.append("классы ответов")
    initial = request["initial_state"]
    checked = set()
    for expectation in reference["expectations"]:
        device, field = expectation["device_id"], expectation["field_id"]
        checked.add((device, field))
        try:
            if not matches_expectation(expectation, initial, final_state):
                failures.append(f"{device}.{field}")
        except (KeyError, TypeError, ValueError):
            failures.append(f"{device}.{field}")
    if reference.get("preserve_other_fields"):
        for device, fields in initial.items():
            for field, value in fields.items():
                if (device, field) not in checked and final_state.get(device, {}).get(field) != value:
                    failures.append(f"{device}.{field} изменено")
    return failures


def evaluate_requests(dataset, reference, answer, room):
    requests = {item["record_id"]: item for item in dataset["requests"]}
    references = {item["record_id"]: item for item in reference["requests"]}
    results = []
    for response in answer["requests"]:
        record_id = response["record_id"]
        request = requests[record_id]
        expected = references[record_id]
        final_state = room.log(response["session_id"])["final_state"]
        failures = compare_request(request, expected, response, final_state)
        results.append({"record_id": record_id, "intent": expected["intent_id"], "failures": failures})
    answered = {item["record_id"] for item in answer["requests"]}
    for record_id, expected in references.items():
        if record_id not in answered:
            results.append({"record_id": record_id, "intent": expected["intent_id"], "failures": ["нет ответа"]})
    return results


def print_report(results):
    passed = sum(not item["failures"] for item in results)
    print(f"Сверка просьб с dev-эталоном: {passed}/{len(results)}")
    groups = defaultdict(lambda: [0, 0])
    for item in results:
        groups[item["intent"]][1] += 1
        groups[item["intent"]][0] += not item["failures"]
    for intent, (correct, total) in sorted(groups.items()):
        print(f"  {intent}: {correct}/{total}")
    for item in results:
        if item["failures"]:
            print(f"  {item['record_id']}: {', '.join(item['failures'])}")
    print("Расписание сна и медицинские формулировки этой сверкой не оцениваются.")


def state_at(initial, steps, instant):
    state = copy.deepcopy(initial)
    for step in sorted(steps, key=lambda item: datetime.fromisoformat(item["at"])):
        if datetime.fromisoformat(step["at"]) > instant:
            break
        state[step["device_id"]].update(step["values"])
    return state


def states_during(initial, steps, start, end):
    if start >= end:
        return []
    checkpoints = {start}
    checkpoints.update(
        datetime.fromisoformat(step["at"])
        for step in steps
        if start < datetime.fromisoformat(step["at"]) < end
    )
    return [state_at(initial, steps, instant) for instant in sorted(checkpoints)]


def check_sleep_rules(stay, reference, initial, steps):
    check_in = datetime.fromisoformat(stay["check_in_at"])
    wake = datetime.fromisoformat(stay["target_wake_at"])
    bedtime = datetime.fromisoformat(reference["resolved_bedtime_at"])
    preference = reference["resolved_preferences"]
    bedtime_start = max(check_in, bedtime)
    night = state_at(initial, steps, bedtime_start)
    evening = states_during(initial, steps, max(check_in, bedtime - timedelta(hours=1)), bedtime)

    dawn_start = wake - timedelta(minutes=30)
    dawn_end = wake - timedelta(minutes=15)
    dawn_steps = [
        step for step in sorted(steps, key=lambda item: datetime.fromisoformat(item["at"]))
        if dawn_start <= datetime.fromisoformat(step["at"]) <= dawn_end
    ]
    brightness_increases = 0
    curtain_opened = False
    first_dawn = None
    for step in dawn_steps:
        moment = datetime.fromisoformat(step["at"])
        before = state_at(initial, steps, moment - timedelta(microseconds=1))
        values = step["values"]
        increased_light = step["device_id"] == "light" and values.get("brightness", before["light"]["brightness"]) > before["light"]["brightness"]
        opened_curtains = step["device_id"] == "curtains" and values.get("position", before["curtains"]["position"]) > before["curtains"]["position"]
        if increased_light:
            brightness_increases += 1
        if opened_curtains:
            curtain_opened = True
        if first_dawn is None and (increased_light or opened_curtains):
            first_dawn = moment

    darkness = states_during(initial, steps, bedtime_start, first_dawn or wake)
    overnight = states_during(initial, steps, bedtime_start, wake)
    low, high = {"cool": (17, 18), "warm": (21, 22)}.get(preference["temperature"], (19, 20))
    sound = night["sound"]
    tracks = preference["favorite_track_ids"]
    alarm = night["alarm"]
    try:
        alarm_matches = datetime.fromisoformat(alarm["at"]) == wake
    except (TypeError, ValueError):
        alarm_matches = False
    dark_step_at_bedtime = any(
        step["device_id"] == "light" and step["values"].get("brightness") == 0
        and datetime.fromisoformat(step["at"]) == bedtime
        for step in steps
    )
    return {
        "R1": all(10 <= state["light"]["brightness"] <= 30 and state["light"]["color_temperature_k"] <= 2700 for state in evening) if evening else None,
        "R2": all(state["light"]["brightness"] == 0 and state["curtains"]["position"] <= 10 for state in darkness) if darkness else None,
        "R3": all(low <= state["climate"]["temperature_c"] <= high for state in overnight) if overnight else None,
        "R4": (sound["track_id"] in tracks and 1 <= sound["volume"] <= 30 and sound["timer_minutes"] <= 45) if tracks else sound["volume"] == 0,
        "R5": alarm_matches and (not preference["gentle_alarm"] or alarm["mode"] == "light"),
        "R6": curtain_opened or brightness_increases >= 3,
        "R7": all(state["dnd"]["enabled"] is True for state in overnight) if overnight else None,
        "R8": dark_step_at_bedtime if bedtime >= check_in else None,
    }


def evaluate_stays(dataset, reference, answer, room):
    stays = {item["record_id"]: item for item in dataset["stays"]}
    references = {item["record_id"]: item for item in reference["stays"]}
    results = []
    for response in answer["stays"]:
        record_id = response["record_id"]
        log = room.log(response["session_id"])
        scenarios = [entry for entry in log["entries"] if entry["kind"] == "scenario"]
        if not scenarios:
            raise ValueError("В журнале заезда отсутствует сценарий")
        latest = max(scenarios, key=lambda entry: entry["revision"])
        rules = check_sleep_rules(stays[record_id], references[record_id], log["session"]["initial_state"], latest["steps"])
        results.append({"record_id": record_id, "rules": rules})
    return results


def print_sleep_report(results):
    print("Приблизительная проверка сценариев по dev-эталону и правилам:")
    for rule in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"):
        checked = [item["rules"][rule] for item in results if item["rules"][rule] is not None]
        passed = sum(value is True for value in checked)
        print(f"  {rule}: {passed}/{len(checked)} проверено из {len(results)}")
    for item in results:
        failed = [rule for rule, passed in item["rules"].items() if passed is False]
        if failed:
            print(f"  {item['record_id']}: {', '.join(failed)}")
    print("R8 здесь проверяется по времени отбоя из dev-эталона; это не официальный оцениватель.")
