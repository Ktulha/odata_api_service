"""HTTP-слой data-API (шаг 10.4): blueprints, авторизация, обработка ошибок.

Маршруты (только GET — сервис read-only по ТЗ):

  GET /api/v1/entities              — каталог доступных роли сущностей;
  GET /api/v1/<entity>/info         — описание таблицы (поля/связи/фильтры/stats);
  GET /api/v1/<entity>              — данные из кеша ($filter/$select/$top/$skip).

Авторизация: Bearer-токен (см. models/auth.py) проверяется НА ЛЮБОМ запросе;
data-токены дают доступ к /api/v1/*, admin-токены — нет (админка — отдельный
blueprint на следующем шаге). Отказ доступа к сущности: unknown → 404,
запрещена глобально или нет grant → 403.

Сессия БД открывается на запрос и закрывается при teardown — ORM-объекты
после выхода из запроса не используются (сервисы возвращают plain dict).
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy.orm import Session

from odata_cache.config import Settings
from odata_cache.entity_info import build_catalog, entity_info
from odata_cache.models.auth import ApiToken, hash_token
from odata_cache.query_service import QueryError, read_entity
from odata_cache.services import access
from odata_cache.services.access import AccessError

logger = logging.getLogger(__name__)

bp = Blueprint("data_api", __name__, url_prefix="/api/v1")


# ---------------------------------------------------------------------------
# инфраструктура запроса
# ---------------------------------------------------------------------------

def _session() -> Session:
    """ORM-сессия запроса (кеш только наша БД; к 1С слой не обращается).

    В тестах engine подменяется через ``app.config["DB_SESSION_FACTORY"]`` —
    боевой путь берёт единый session factory процесса из db/engine.py.
    """
    if "db_session" not in g:
        factory = current_app.config.get("DB_SESSION_FACTORY")
        if factory is None:
            from odata_cache.db.engine import get_session_factory

            factory = get_session_factory()
        g.db_session = factory()
    return g.db_session


@bp.teardown_request
def _close_session(exc):  # pragma: no cover - тривиальный cleanup
    s = g.pop("db_session", None)
    if s is not None:
        s.close()


def _error(status: int, code: str, message: str, detail: dict | None = None):
    body: dict = {"errors": [{"code": code, "message": message}]}
    if detail:
        body["errors"][0].update(detail)
    return jsonify(body), status


# ---------------------------------------------------------------------------
# Bearer-авторизация (проверяется при любом запросе к /api/v1/*)
# ---------------------------------------------------------------------------

def require_data_token() -> ApiToken:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise AccessError("no_token")
    secret = header[len("Bearer "):].strip()
    if not secret:
        raise AccessError("no_token")

    session = _session()
    token = session.query(ApiToken).filter_by(token_hash=hash_token(secret)).one_or_none()
    if token is None or not token.is_valid():
        raise AccessError("bad_token")
    if token.kind != "data":
        raise AccessError("wrong_kind")
    g.api_token = token
    return token


@bp.before_request
def authenticate():
    """/healthz и OPTIONS не требуют токен; всё остальное — требуют."""
    if request.method == "OPTIONS":
        return None
    try:
        require_data_token()
    except AccessError as e:
        status = 401 if e.reason in ("no_token", "bad_token", "wrong_kind") else 403
        return _error(status, e.reason, "Требуется валидный Bearer data-токен")
    return None


# маппинг причин отказа доступа к сущности → HTTP
_ACCESS_STATUS = {"unknown": 404, "forbidden": 403, "not_granted": 403,
                  "not_cacheable": 403}


# ---------------------------------------------------------------------------
# маршруты
# ---------------------------------------------------------------------------

@bp.get("/entities")
def list_entities():
    """Каталог сущностей, доступных роли текущего токена."""
    token = g.api_token
    session = _session()
    names = access.allowed_entities(session, token.role_id)
    entities = [e for e in session.scalars(
        select_all_entities(session)) if e.name in names]
    return jsonify({"data": build_catalog(session, entities)})


def select_all_entities(session: Session):
    from sqlalchemy import select

    from odata_cache.models.registry import Entity

    return select(Entity).order_by(Entity.name)


@bp.get("/<entity_name>/info")
def get_info(entity_name: str):
    token = g.api_token
    session = _session()
    try:
        ent = access.require_entity_access(
            session, name=entity_name, role_id=token.role_id)
    except AccessError as e:
        return _error(_ACCESS_STATUS.get(e.reason, 403), e.reason,
                      f"Сущность '{entity_name}' недоступна")
    with_stats = (request.args.get("stats") or "1").strip().lower() in ("1", "true", "yes")
    return jsonify(entity_info(session, ent, with_stats=with_stats))


@bp.get("/<entity_name>")
def get_data(entity_name: str):
    token = g.api_token
    session = _session()
    params = {k: v for k, v in request.args.items(multi=False)}
    try:
        result = read_entity(session, entity_name=entity_name,
                             role_id=token.role_id, params=params)
    except AccessError as e:
        return _error(_ACCESS_STATUS.get(e.reason, 403), e.reason,
                      f"Сущность '{entity_name}' недоступна")
    except QueryError as e:
        return _error(400, "bad_query", e.message, e.detail)
    return jsonify(result)
