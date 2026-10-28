"""Каркас Flask-приложения (шаг 2).

Factory-паттерн: ``create_app(mode)`` собирает приложение под конкретную роль:

- ``api``    — только data-API (/api/v1/*), планировщик выключен;
- ``admin``  — только админка (/admin/v1/*);
- ``all``    — всё вместе (разработка/малые инсталляции);
- ``worker`` — без HTTP, планировщик + синхронизация (запуск через CLI).

Тесты могут вызывать factory напрямую с подменёнными настройками.
"""

from flask import Flask, jsonify

from odata_cache.config import Settings, get_settings


def create_app(mode: str = "all", settings: Settings | None = None) -> Flask:
    if mode not in {"api", "admin", "all", "worker"}:
        raise ValueError(f"неизвестный режим запуска: {mode!r}")

    app = Flask(__name__)
    app.config["MODE"] = mode
    app.config["SETTINGS"] = settings or get_settings()

    register_health(app)

    # Blueprints data/admin подключим на шагах 5, 6 и 10:
    # from odata_cache.api.data import bp as data_bp
    # from odata_cache.api.admin import bp as admin_bp
    return app


def register_health(app: Flask) -> None:
    """Служебный эндпоинт без авторизации — для health-check оркестратора."""

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "mode": app.config["MODE"]})
