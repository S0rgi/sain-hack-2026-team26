"""Создать dev-прогон и передать его токен локальному приложению."""

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dev_eval import evaluate_requests, evaluate_stays, print_report, print_sleep_report
from room_client import RoomClient, read_json


def post(url, token, payload):
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=2700) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description="Запуск dev-прогона кейса 2")
    parser.add_argument("--evaluate", action="store_true", help="Проверить формат и сравнить просьбы с открытым эталоном")
    args = parser.parse_args()
    base_url = os.environ["HACK_API_URL"].rstrip("/")
    team_token = os.environ["HACK_TEAM_TOKEN"]
    app_url = os.environ.get("APP_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        run = post(base_url + "/runs", team_token, {"case": 2})
    except HTTPError as exc:
        print(f"Не удалось создать dev-прогон. API организаторов вернул HTTP {exc.code}.")
        raise SystemExit(1)
    try:
        result = post(app_url + "/run", run["run_token"], {"case": 2, "dataset_code": run["dataset_id"]})
    except HTTPError as exc:
        print(f"Локальный сервер вернул HTTP {exc.code} при обработке набора.")
        if exc.code == 503:
            print("Проверьте HACK_API_URL в окне, где запущен сервер.")
        raise SystemExit(1)
    print(f"Обработано заездов: {len(result['stays'])}, просьб: {len(result['requests'])}")
    if args.evaluate:
        validation = post(base_url + "/case/2/validate", team_token, result)
        print(f"Проверка формата: {validation['valid']}, ошибок: {len(validation['errors'])}")
        query = urlencode({"dataset_code": run["dataset_id"]})
        request = Request(base_url + "/case/2/getData?" + query, headers={"Authorization": f"Bearer {team_token}"})
        data = read_json(request, timeout=30, attempts=3)
        if data.get("reference") is None:
            raise ValueError("Dev-эталон не получен")
        room = RoomClient(base_url, run["run_token"])
        print_report(evaluate_requests(data["dataset"], data["reference"], result, room))
        print_sleep_report(evaluate_stays(data["dataset"], data["reference"], result, room))


if __name__ == "__main__":
    try:
        main()
    except KeyError as exc:
        print(f"Отсутствует обязательное поле или переменная: {exc}")
        raise SystemExit(1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"Запрос не выполнен: {type(exc).__name__}")
        raise SystemExit(1)
