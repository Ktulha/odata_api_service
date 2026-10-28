"""Сохранить сниппет реальных данных 1С в fixture.

Использование:
  python fixtures/save_sample.py <имя_сущности> < input.json
или
  python fixtures/save_sample.py <имя_сущности> --url "http://host/odata/standard.odata/Catalog_X?$top=50&$format=json"

Файл: fixtures/<имя>.json — сырой ответ 1С (как есть, с оdata.metadata).
"""
import argparse, json, re, sys
from pathlib import Path

def sanitize(obj):
    """Удалить потенциально чувствительные поля из JSON (рекурсивно)."""
    SENSITIVE = re.compile(r"(пароль|password|secret|токен|token|ИНН|фио|email)", re.I)
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if not SENSITIVE.search(k)} | \
               {k: sanitize(v) for k, v in obj.items() if SENSITIVE.search(k) and False}
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    return obj

def main():
    p = argparse.ArgumentParser()
    p.add_argument("entity")
    p.add_argument("--url", help="URL для загрузки (иначе читаем stdin)")
    p.add_argument("--no-sanitize", action="store_true")
    a = p.parse_args()
    if a.url:
        import httpx
        r = httpx.get(a.url, timeout=60, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
    else:
        data = json.load(sys.stdin)
    if not a.no_sanitize:
        data = sanitize(data)
    out = Path(__file__).parent / f"{a.entity}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    n = len(data.get("value", [])) if isinstance(data, dict) else "?"
    print(f"OK: {out} ({n} записей)")

if __name__ == "__main__":
    main()
