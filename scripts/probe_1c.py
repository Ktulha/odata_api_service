"""Диагностика подключения к 1С OData (read-only, ничего не пишет в БД).

Использование:
    uv run python scripts/probe_1c.py                     # берёт ODATA_ONEDATA_* из .env
    uv run python scripts/probe_1c.py --base-url http://192.168.14.152/unf_14062023/odata/standard.odata \
        --user sync_user --password 'secret'
    uv run python scripts/probe_1c.py --entity Catalog_Номенклатура --top 3

Проверяет по порядку:
    1) доступность корня публикации;
    2) $metadata с basic-auth;
    3) парсинг $metadata нашим parser'ом + список EntitySet;
    4) одну страницу данных выбранной сущности ($top=N, $format=json).

Коды выхода: 0 — всё OK; 1 — ошибка на одном из шагов (шаг указан в логе).
"""

from __future__ import annotations

import argparse
import os
import sys

# Добавляем src/ в путь, чтобы запускать без установки пакета.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx  # noqa: E402

from odata_cache.metadata import parse_metadata  # noqa: E402


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    p = argparse.ArgumentParser(description="probe 1C OData publication")
    p.add_argument("--base-url", default=os.environ.get("ODATA_ONEDATA_BASE_URL", ""),
                   help="например http://host/base/odata/standard.odata (из .env ODATA_ONEDATA_BASE_URL)")
    p.add_argument("--user", default=os.environ.get("ODATA_ONEDATA_USERNAME", ""))
    p.add_argument("--password", default=os.environ.get("ODATA_ONEDATA_PASSWORD", ""))
    p.add_argument("--timeout", type=float, default=float(os.environ.get("ODATA_ONEDATA_TIMEOUT", 30)))
    p.add_argument("--entity", default="", help="имя EntitySet для проверки данных (по умолчанию первая)")
    p.add_argument("--top", type=int, default=5, help="сколько записей скачать для пробы")
    p.add_argument("--insecure", action="store_true", help="не проверять TLS-сертификат (самоподписанные 1С)")
    args = p.parse_args()

    if not args.base_url:
        print("ОШИБКА: не задан --base-url и нет ODATA_ONEDATA_BASE_URL в окружении/.env")
        return 1

    client = httpx.Client(base_url=args.base_url.rstrip("/"),
                          auth=(args.user, args.password) if args.user else None,
                          timeout=args.timeout, follow_redirects=True, verify=not args.insecure)

    # --- шаг 1: корень публикации -----------------------------------------
    step(f"1. GET {args.base_url}/ (корень)")
    try:
        r = client.get("/")
        print(f"HTTP {r.status_code}; сервер: {r.headers.get('server', '?')}")
        if r.status_code in (401, 403):
            print("Сервер требует аутентификацию — передаём basic-auth дальше.")
    except httpx.HTTPError as e:
        print(f"ОШИБКА соединения: {e.__class__.__name__}: {e}")
        print("Проверьте адрес/порт/доступность базы; для самоподписанного HTTPS — флаг --insecure.")
        return 1

    # --- шаг 2: $metadata ---------------------------------------------------
    step("2. GET $metadata")
    try:
        r = client.get("/$metadata", headers={"Accept": "application/xml"})
        print(f"HTTP {r.status_code}, {len(r.text)} байт")
        if r.status_code != 200:
            print(r.text[:500])
            return 1
    except httpx.HTTPError as e:
        print(f"ОШИБКА: {e}")
        return 1

    # --- шаг 3: разбор через наш парсер -------------------------------------
    step("3. Парсинг $metadata (odata_cache.metadata)")
    try:
        model = parse_metadata(r.text)
        names = model.cacheable_entity_names()
        print(f"EntitySet (кэшируемых): {len(names)}")
        for n in names[:30]:
            et = model.entity_type_for_set(n)
            nk = len(et.key_properties) if et else 0
            print(f"  - {n} (key props: {nk})")
        if len(names) > 30:
            print(f"  ... ещё {len(names) - 30}")
    except Exception as e:
        print(f"ОШИБКА парсинга: {e.__class__.__name__}: {e}")
        print("Пришлите фрагмент $metadata в чат — расширим парсер.")
        return 1

    # --- шаг 4: данные ------------------------------------------------------
    entity = args.entity or (names[0] if names else "")
    if not entity:
        print("Нет ни одного EntitySet — проверку данных пропускаем.")
        return 0
    step(f"4. GET {entity}?$top={args.top}&$format=json")
    try:
        r = client.get(f"/{entity}", params={"$top": args.top, "$format": "json"})
        print(f"HTTP {r.status_code}")
        if r.status_code != 200:
            print(r.text[:500])
            return 1
        data = r.json()
        rows = data.get("value", [])
        print(f"Записей в странице: {len(rows)}")
        if rows:
            keys = list(rows[0].keys())
            print(f"Полей в записи: {len(keys)}; первые 10: {keys[:10]}")
    except Exception as e:
        print(f"ОШИБКА: {e.__class__.__name__}: {e}")
        return 1

    print("\nOK: все шаги пройдены. Можно настраивать worker/Alembic (следующие шаги).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
