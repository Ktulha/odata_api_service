"""CLI-точка входа (шаг 2, расширен на шаге 3).

Запуск:
    uv run odata-api serve --mode api      # HTTP API (gunicorn в prod)
    uv run odata-api db init               # создать служебные таблицы (dev/тесты)
    uv run odata-api worker                # планировщик + синхронизация (шаг 9)
"""

import click

from odata_cache.app_factory import create_app


@click.group()
def cli() -> None:
    """ics-odata-api: read-only кеш 1С OData."""


@cli.command("serve")
@click.option("--mode", default="all", help="api | admin | all")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8000, type=int)
def serve(mode: str, host: str, port: int) -> None:
    app = create_app(mode)
    app.run(host=host, port=port)


@cli.group("db")
def db_group() -> None:
    """Служебные операции над БД кеша."""


@db_group.command("init")
def db_init() -> None:
    """Создать отсутствующие служебные таблицы (без ALTER; prod — Alembic)."""
    from odata_cache.db import init_db

    init_db()
    click.echo("OK: служебные таблицы созданы/проверены")


if __name__ == "__main__":
    cli()
