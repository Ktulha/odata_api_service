"""Управление реестром и синхронизацией из CLI (шаги 3/8, worker-часть).

Команды:
  odata-api sync-metadata        — скачать $metadata → БД → разобрать в реестр;
  odata-api entities             — список сущностей реестра;
  odata-api enable <name>        — включить кеширование (+ расписание по умолчанию);
  odata-api disable <name>       — выключить (данные чистятся отдельно);
  odata-api schedule <name> <cron|--remove> [tz] — правка строки sync_jobs;
  odata-api schedules            — таблица расписаний;
  odata-api sync <name>          — ручная полная синхронизация (worker_sync);
  odata-api worker               — планировщик APScheduler + подбор lazy-очереди.

Реквизиты 1С читаются из env (.env), в аргументах CLI не светятся.
"""

from __future__ import annotations

import logging

import click
from sqlalchemy import select

from odata_cache.db.engine import SessionLocal, get_engine, init_db
from odata_cache.models.registry import Entity
from odata_cache.models.sync import SyncJob


@click.group()
def registry_cli() -> None:
    """Управление реестром сущностей и синхронизацией."""


@registry_cli.command("sync-metadata")
def cmd_sync_metadata() -> None:
    """Скачать $metadata из 1С, сохранить в БД и разложить в реестр."""
    from odata_cache.config import get_settings
    from odata_cache.metadata_parser import parse_metadata_xml
    from odata_cache.models.odata_metadata import ODataMetadata
    from odata_cache.services.registry_service import sync_metadata_to_registry
    from odata_cache.worker.odata_client import OdataClient

    settings = get_settings()
    if not settings.onedata_base_url:
        raise click.ClickException("не задан ODATA_ONEDATA_BASE_URL")
    with OdataClient(
        settings.onedata_base_url,
        auth_mode="basic" if settings.onedata_username else "none",
        username=settings.onedata_username,
        password=settings.onedata_password,
        timeout=settings.onedata_timeout,
    ) as client:
        xml = client.get_metadata()

    model = parse_metadata_xml(xml)
    with SessionLocal() as session:
        meta_row = session.scalar(
            select(ODataMetadata).order_by(ODataMetadata.id.desc())
        )
        if meta_row is None:
            meta_row = ODataMetadata(raw_xml=xml)
            session.add(meta_row)
        else:
            meta_row.raw_xml = xml
        result = sync_metadata_to_registry(session, model)
        session.commit()
    click.echo(f"сущностей: всего {result.total}, новых {result.new}, "
               f"обновлено {result.updated}, удалено {len(result.removed)}")
    for name in result.removed:
        click.echo(f"  снята из реестра: {name}")


@registry_cli.command("entities")
def cmd_entities() -> None:
    """Показать реестр сущностей."""
    with SessionLocal() as session:
        rows = list(session.scalars(select(Entity).order_by(Entity.name)))
    for e in rows:
        click.echo(f"{e.name:60} state={e.state:10} active={int(e.is_active)} "
                   f"props={len(e.properties)} synced={e.last_synced_at or '-'}")
    click.echo(f"итого: {len(rows)}")


@registry_cli.command("enable")
@click.argument("name")
@click.option("--no-schedule", is_flag=True, help="не ставить cron-расписание")
def cmd_enable(name: str, no_schedule: bool) -> None:
    """Включить кеширование сущности (state=scheduled) и создать таблицу."""
    from odata_cache.config import get_settings
    from odata_cache.services import access, ddl

    init_db()
    with SessionLocal() as session:
        ent = access.enable_caching(session, name, via_schedule=not no_schedule)
        if not no_schedule:
            _ensure_job(session, ent, get_settings().sync_default_cron)
        props = list(ent.properties)
        session.commit()
    if props:
        engine = get_engine()
        ddl.ensure_table(engine, ent, props)
        click.echo(f"включено: {name}; таблица {ent.storage_table} готова")
    else:
        click.echo(f"включено: {name} (свойств в реестре нет — сначала sync-metadata)")


@registry_cli.command("disable")
@click.argument("name")
def cmd_disable(name: str) -> None:
    """Выключить кеширование: state=disabled, снять расписание."""
    from odata_cache.services import access

    with SessionLocal() as session:
        ent = access.disable_caching(session, name)
        job = session.scalar(select(SyncJob).where(SyncJob.entity_id == ent.id))
        if job is not None:
            job.enabled = False
        session.commit()
    click.echo(f"выключено: {name} (данные из таблиц нужно убрать отдельно)")


@registry_cli.command("schedule")
@click.argument("name")
@click.argument("cron_or_flag", metavar="CRON|--remove")
@click.argument("timezone", required=False, default="UTC")
def cmd_schedule(name: str, cron_or_flag: str, timezone: str) -> None:
    """Задать/снять cron-расписание синхронизации сущности (таблица sync_jobs)."""
    from odata_cache.services.access import get_entity
    from odata_cache.worker.scheduler import validate_cron

    with SessionLocal() as session:
        ent = get_entity(session, name)
        if ent is None:
            raise click.ClickException(f"сущность {name!r} не найдена в реестре")
        job = session.scalar(select(SyncJob).where(SyncJob.entity_id == ent.id))
        if cron_or_flag == "--remove":
            if job is not None:
                session.delete(job)
            session.commit()
            click.echo(f"расписание {name} снято")
            return
        try:
            validate_cron(cron_or_flag)
        except ValueError as e:
            raise click.ClickException(str(e)) from e
        if job is None:
            job = SyncJob(entity_id=ent.id, cron=cron_or_flag, timezone=timezone)
            session.add(job)
        else:
            job.cron, job.timezone, job.enabled = cron_or_flag, timezone, True
        session.commit()
        click.echo(f"расписание {name}: '{cron_or_flag}' ({timezone}), job #{job.id}")


@registry_cli.command("schedules")
def cmd_schedules() -> None:
    """Таблица расписаний sync_jobs."""
    with SessionLocal() as session:
        rows = session.execute(
            select(SyncJob, Entity.name)
            .join(Entity, Entity.id == SyncJob.entity_id)
            .order_by(Entity.name)
        ).all()
    for job, name in rows:
        click.echo(f"{name:55} cron='{job.cron}' tz={job.timezone} "
                   f"enabled={int(job.enabled)} next={job.next_run_at or '-'} "
                   f"last={job.last_run_at or '-'}")
    if not rows:
        click.echo("(расписаний нет)")


@registry_cli.command("sync")
@click.argument("name")
def cmd_sync(name: str) -> None:
    """Ручная полная синхронизация сущности (как это делает планировщик)."""
    from odata_cache.worker.scheduler import SyncService

    stats = SyncService().run_entity(name, trigger="manual")
    click.echo(f"{name}: задач ok={stats['tasks_ok']} fail={stats['tasks_failed']} "
               f"fetched={stats['fetched']} inserted={stats['inserted']} "
               f"updated={stats['updated']} deleted={stats['deleted']}")
    for w in stats["warnings"]:
        click.echo(f"  warning: {w}", err=True)
    for e in stats["errors"]:
        click.echo(f"  error: {e}", err=True)
    if stats["skipped_forbidden"]:
        click.echo(f"  пропущены запрещённые: {', '.join(stats['skipped_forbidden'])}")
    if stats["tasks_failed"]:
        raise SystemExit(1)


@registry_cli.command("worker")
@click.option("--refresh-sec", default=60, show_default=True,
              help="период перечитывания таблицы расписаний")
def cmd_worker(refresh_sec: int) -> None:
    """Запустить воркер: планировщик синхронизаций + подбор lazy-очереди."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from odata_cache.worker.scheduler import Scheduler

    init_db()
    sched = Scheduler(refresh_sec=refresh_sec)
    sched.run_forever()


def _ensure_job(session: SessionLocal, ent: Entity, cron: str) -> SyncJob:
    """Создать/включить строку расписания для сущности (идемпотентно)."""
    job = session.scalar(select(SyncJob).where(SyncJob.entity_id == ent.id))
    if job is None:
        job = SyncJob(entity_id=ent.id, cron=cron)
        session.add(job)
    else:
        job.enabled = True
    return job
