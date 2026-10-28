"""Планировщик синхронизации worker'а (шаг C.4).

Расписание живёт в БД (таблица ``sync_jobs``, согласованный вариант «а»):
админка пишет cron-выражение, worker каждые ``refresh_sec`` секунд перечитывает
таблицу и приводит набор задач APScheduler в соответствие (add/modify/remove) —
без рестарта процесса. Удалённый/выключенный в админке job исчезает из
планировщика при ближайшем refresh.

APScheduler 3.x: BackgroundScheduler + пул потоков; на сущность — одна задача
с ``coalesce=True`` и ``max_instances=1`` (пропущенные запуски схлопываются,
параллельные прогоны одной сущности запрещены). Временная зона — из колонки
``sync_jobs.timezone`` (IANA-имя, напр. Europe/Moscow).

Для unit-тестов :class:`SyncService` подменяется заглушкой — планировщик
проверяется без сети и данных (tests/test_scheduler.py).
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from odata_cache.config import Settings, get_settings
from odata_cache.db.engine import SessionLocal, get_engine
from odata_cache.models.registry import Entity
from odata_cache.models.sync import SyncJob
from odata_cache.worker.odata_client import OdataClient
from odata_cache.worker.worker_sync import utcnow_iso

logger = logging.getLogger(__name__)

APS_PREFIX = "sync:"  # префикс id задачи в APScheduler


def validate_cron(expr: str) -> None:
    """Проверка cron-выражения (5 полей) — используется админкой и CLI."""
    from croniter import croniter

    try:
        croniter(expr.strip())
    except Exception as e:  # noqa: BLE001 — любой разборщик отклонён
        raise ValueError(f"некорректный cron {expr!r}: {e}") from e


def _aps_id(entity_name: str) -> str:
    return APS_PREFIX + entity_name


class SyncService:
    """Один полный прогон синхронизации сущности (клиент → worker_sync)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def make_client(self) -> OdataClient:
        s = self.settings
        return OdataClient(
            s.onedata_base_url,
            auth_mode="basic" if s.onedata_username else "none",
            username=s.onedata_username,
            password=s.onedata_password,
            timeout=s.onedata_timeout,
            verify_tls=not getattr(s, "onedata_insecure_tls", False),
        )

    def run_entity(self, entity_name: str, *, trigger: str = "schedule",
                   job_id: int | None = None) -> dict:
        """Синхронизировать сущность и её разрешённые зависимости.

        Открывает собственные session/engine — метод вызывается из потока
        APScheduler, поэтому никаких общих сессий держать не должен.
        """
        from odata_cache.worker.worker_sync import sync_entity

        engine = get_engine()
        with SessionLocal() as session:
            client = self.make_client()
            try:
                stats = sync_entity(session, engine, client, entity_name,
                                    mode="full", trigger=trigger, job_id=job_id)
                session.commit()
                return stats
            except Exception:
                session.rollback()
                raise
            finally:
                client.close()


class Scheduler:
    """Обёртка над BackgroundScheduler: расписание читается из sync_jobs."""

    def __init__(self, service: SyncService | None = None,
                 settings: Settings | None = None,
                 refresh_sec: int = 60) -> None:
        self.settings = settings or get_settings()
        self.service = service or SyncService(self.settings)
        self.refresh_sec = refresh_sec
        self._sched = BackgroundScheduler(
            executors={"default": {"type": "pool", "max_workers": 2}},
            job_defaults={"coalesce": True, "max_instances": 1,
                          "misfire_grace_time": 300},
        )
        # aps-id → (sync_job.id, cron, timezone): кэш для diff при refresh
        self._known: dict[str, tuple[int, str, str]] = {}
        self._lock = threading.Lock()
        self._lazy_running = False  # защита от параллельного подбора очереди

    # ------------------------------------------------------------ постановка
    def _sync_one(self, job: SyncJob, entity_name: str) -> None:
        aid = _aps_id(entity_name)
        spec = (job.id, job.cron.strip(), (job.timezone or "UTC").strip())
        with self._lock:
            if self._known.get(aid) == spec and self._sched.get_job(aid):
                return  # без изменений — не трогаем next_run
            self._remove_locked(aid)
            try:
                trigger = CronTrigger.from_crontab(spec[1], timezone=ZoneInfo(spec[2]))
            except (ValueError, ZoneInfoNotFoundError) as e:
                logger.error("job %s: cron %r / tz %r отклонён (%s) — пропущен",
                             job.id, spec[1], spec[2], e)
                return
            self._sched.add_job(
                self._run_and_log, trigger=trigger, id=aid,
                args=[entity_name, job.id], name=f"sync {entity_name}",
            )
            self._known[aid] = spec
            sched_job = self._sched.get_job(aid)
            self._mark_next(job.id, sched_job.next_run_time if sched_job else None)
            logger.info("запланировано: %s [%s @ %s] следующий запуск %s",
                        entity_name, spec[1], spec[2],
                        sched_job.next_run_time if sched_job else "?")

    def _remove_locked(self, aid: str) -> None:
        self._known.pop(aid, None)
        if self._sched.get_job(aid):
            self._sched.remove_job(aid)

    def _mark_next(self, sync_job_id: int, when: dt.datetime | None) -> None:
        with SessionLocal() as session:
            job = session.get(SyncJob, sync_job_id)
            if job is not None:
                job.next_run_at = (when.astimezone(dt.timezone.utc)
                                   .isoformat(timespec="seconds")) if when else None
                session.commit()

    # ------------------------------------------------------------ выполнение
    def _run_and_log(self, entity_name: str, sync_job_id: int) -> None:
        """Тело задачи в пуле потоков: прогон + отметка last_run_at."""
        started = utcnow_iso()
        try:
            stats = self.service.run_entity(entity_name, trigger="schedule",
                                            job_id=sync_job_id)
            logger.info("sync %s: ok=%d fail=%d fetched=%d", entity_name,
                        stats["tasks_ok"], stats["tasks_failed"], stats["fetched"])
        except Exception:  # noqa: BLE001 — поток планировщика не должен умирать
            logger.exception("sync %s: критический сбой прогона", entity_name)
        finally:
            with SessionLocal() as session:
                job = session.get(SyncJob, sync_job_id)
                if job is not None:
                    job.last_run_at = started
                    session.commit()

    def trigger_now(self, entity_name: str) -> None:
        """Ручной запуск из админки: немедленная разовая задача в том же пуле."""
        self._sched.add_job(
            self._manual_run, args=[entity_name],
            id=f"manual:{entity_name}:{time.time():.3f}",
            name=f"manual sync {entity_name}",
        )

    def _manual_run(self, entity_name: str) -> None:
        try:
            self.service.run_entity(entity_name, trigger="manual")
        except Exception:
            logger.exception("ручной sync %s упал", entity_name)

    # ------------------------------------------------------------ очередь lazy
    def process_lazy_queue(self) -> int:
        """Подобрать pending-заявки lazy_load_requests и выполнить их.

        Возвращает число обработанных заявок. Заявка помечается running до
        старта — повторный подбор её не схватит; при сбое процесса заявка
        остаётся running и будет разобрана вручную/при следующем старте
        (консервативно: не дерём 1С дважды автоматически).
        """
        if self._lazy_running:  # подбор уже идёт в другом потоке — не лезем
            return 0
        self._lazy_running = True
        try:
            return self._process_lazy_locked()
        finally:
            self._lazy_running = False

    def _process_lazy_locked(self) -> int:
        from sqlalchemy import select as _select

        from odata_cache.models.sync import LazyLoadRequest

        handled = 0
        with SessionLocal() as session:
            rows = list(session.scalars(
                _select(LazyLoadRequest)
                .where(LazyLoadRequest.status == "pending")
                .order_by(LazyLoadRequest.id)
            ))
        for req in rows:
            with SessionLocal() as session:
                r = session.get(LazyLoadRequest, req.id)
                if r is None or r.status != "pending":
                    continue
                ent = session.get(Entity, r.entity_id)
                if ent is None:
                    r.status, r.finished_at = "failed", utcnow_iso()
                    r.error = "сущность удалена из реестра"
                    session.commit()
                    continue
                name = ent.name
                r.status, r.claimed_at = "running", utcnow_iso()
                session.commit()
            try:
                self.service.run_entity(name, trigger="startup")
                final, err = "done", None
            except Exception as e:  # noqa: BLE001
                logger.exception("lazy sync %s упал", name)
                final, err = "failed", str(e)[:2000]
            with SessionLocal() as session:
                r = session.get(LazyLoadRequest, req.id)
                if r is not None:
                    r.status, r.finished_at, r.error = final, utcnow_iso(), err
                    session.commit()
            handled += 1
        return handled

    # ------------------------------------------------------------ цикл
    def refresh(self) -> None:
        """Перечитать sync_jobs и привести планировщик в соответствие БД."""
        wanted: set[str] = set()
        with SessionLocal() as session:
            rows = session.execute(
                select(SyncJob, Entity.name)
                .join(Entity, Entity.id == SyncJob.entity_id)
                .where(SyncJob.enabled.is_(True), Entity.is_active.is_(True))
            ).all()
        for job, ent_name in rows:
            self._sync_one(job, ent_name)
            wanted.add(_aps_id(ent_name))
        with self._lock:
            for aid in list(self._known):
                if aid not in wanted:
                    logger.info("снято с расписания: %s", aid)
                    self._remove_locked(aid)

    def start(self) -> None:
        self._sched.start()

    def shutdown(self) -> None:
        self._sched.shutdown(wait=False)

    def run_forever(self) -> None:
        """Главный цикл CLI-команды ``odata-api worker``.

        Быстрый тик (lazy_poll_sec) подбирает очередь ленивой загрузки;
        раз в refresh_sec перечитывается расписание sync_jobs.
        """
        self._sched.start()
        logger.info("worker запущен (расписание из sync_jobs, refresh %ds, lazy %ds)",
                    self.refresh_sec, self.settings.lazy_poll_sec)
        last_refresh = 0.0
        try:
            while True:
                now = time.monotonic()
                if now - last_refresh >= self.refresh_sec:
                    last_refresh = now
                    try:
                        self.refresh()
                    except Exception:  # noqa: BLE001 — БД моргнула, переживём
                        logger.exception("refresh расписания упал")
                if getattr(self.settings, "lazy_load_enabled", True):
                    try:
                        self.process_lazy_queue()
                    except Exception:  # noqa: BLE001
                        logger.exception("подбор lazy-очереди упал")
                time.sleep(self.settings.lazy_poll_sec)
        except KeyboardInterrupt:  # pragma: no cover
            logger.info("остановка по Ctrl+C")
        finally:
            self.shutdown()
