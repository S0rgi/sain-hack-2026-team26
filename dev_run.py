"""Создать dev-прогон и передать его токен локальному приложению."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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


if __name__ == "__main__":
    try:
        main()
    except KeyError as exc:
        print(f"Отсутствует обязательное поле или переменная: {exc}")
        raise SystemExit(1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"Запрос не выполнен: {type(exc).__name__}")
        raise SystemExit(1)
