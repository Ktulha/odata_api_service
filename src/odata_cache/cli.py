"""CLI-точка входа (шаг 2, расширен на шаге 3, 8).

Запуск:
    uv run odata-api serve --mode api      # HTTP API (gunicorn в prod)
    uv run odata-api db init               # создать служебные таблицы (dev/тесты)
    uv run odata-api sync-metadata         # $metadata из 1С -> реестр сущностей (БД)
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


@cli.command("sync-metadata")
@click.option("--save-file", default="", help="дополнительно сохранить сырой $metadata в файл (для отладки)")
def sync_metadata_cmd(save_file: str) -> None:
    """Загрузить $metadata из 1С и разложить в реестр сущностей (БД кеша).

    Единственная CLI-команда, которая обращается к 1С напрямую (роль worker).
    Данные сущностей при этом НЕ грузятся — только схема.
    """
    import sys

    from odata_cache.config import get_settings
    from odata_cache.db import session_scope
    from odata_cache.metadata import parse_metadata
    from odata_cache.services import registry_service

    s = get_settings()
    if not s.onedata_base_url:
        click.echo("ОШИБКА: не задан ODATA_ONEDATA_BASE_URL (.env)", err=True)
        sys.exit(1)

    import httpx

    url = s.onedata_base_url.rstrip("/") + "/$metadata"
    click.echo(f"GET {url}")
    try:
        r = httpx.get(url, auth=(s.onedata_username, s.onedata_password),
                      timeout=s.onedata_timeout, follow_redirects=True,
                      headers={"Accept": "application/xml"})
        r.raise_for_status()
    except httpx.HTTPError as e:
        click.echo(f"ОШИБКА соединения с 1С: {e.__class__.__name__}: {e}", err=True)
        sys.exit(2)

    xml_text = r.text
    if save_file:
        with open(save_file, "w", encoding="utf-8") as f:
            f.write(xml_text)
        click.echo(f"сохранено: {save_file} ({len(xml_text)} байт)")

    model = parse_metadata(xml_text)
    with session_scope() as session:
        res = registry_service.sync_metadata_to_registry(session, model)
        resolved = registry_service.resolve_all_ref_targets(session)
    click.echo(
        f"Реестр: создано {len(res.created)}, обновлено {len(res.updated)}, "
        f"без изменений {len(res.unchanged)}, удалено {len(res.removed)}; "
        f"связей разрешено: {resolved}"
    )


if __name__ == "__main__":
    cli()
