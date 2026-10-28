"""OData-клиент worker'а (шаг C.1).

Единственный модуль проекта, который ходит в 1С за ДАННЫМИ. Код data/admin API
его не импортирует и не использует — прямые OData-запросы извне невозможны по
архитектуре (ТЗ п.1.2 «Безопасность»).

Возможности:
- $metadata (сырой XML) и чтение коллекций EntitySet;
- basic / bearer / none аутентификация (1С: OData.publish обычно basic);
- пагинация: явная ($skiptoken из odata.nextLink) и fallback $skip/$top;
- обход odata.nextLink с защитой от зацикливания;
- типизированная ошибка OdataError (HTTP-код + фрагмент ответа) без утечки
  секретов (пароль/токен в текст ошибки не попадают);
- корректный percent-encoding кириллических имён сущностей в URL.

Модуль синхронный (httpx.Client): worker — отдельный процесс, фоновые задачи
APScheduler выполняются в пуле потоков, asyncio здесь только усложнил бы код.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator
from urllib.parse import quote, urlsplit, urlunsplit, parse_qsl, urlencode

import httpx

logger = logging.getLogger(__name__)


class OdataError(RuntimeError):
    """Ошибка обращения к 1С. code — HTTP-статус или -1 (сетевая/прочее)."""

    def __init__(self, message: str, *, code: int = -1) -> None:
        super().__init__(message)
        self.code = code


class OdataClient:
    """Клиент одного подключения к 1С OData.

    base_url — корень сервиса, напр. ``http://host/base/odata/standard.odata``
    (без завершающего слэша). Путь ``/{EntitySet}`` добавляется методами.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth_mode: str = "basic",          # basic | bearer | none
        username: str = "",
        password: str = "",
        token: str = "",
        timeout: float = 30.0,
        verify_tls: bool = True,
        page_size: int = 1000,
        max_pages: int = 10_000,
    ) -> None:
        if not base_url:
            raise ValueError("base_url обязателен для OdataClient")
        self.base_url = base_url.rstrip("/")
        self.auth_mode = auth_mode
        self.timeout = timeout
        self.page_size = max(1, int(page_size))
        self.max_pages = max(1, int(max_pages))

        auth: tuple[str, str] | None = (username, password) if auth_mode == "basic" else None
        headers = {"Accept": "application/json"}
        if auth_mode == "bearer":
            if not token:
                raise ValueError("bearer-режим требует token")
            headers["Authorization"] = f"Bearer {token}"

        self._client = httpx.Client(
            auth=auth,
            headers=headers,
            timeout=timeout,
            verify=verify_tls,
            follow_redirects=True,
        )
        # URL последнего запроса — база для склейки относительного nextLink
        self._last_url: str | None = None

    # ------------------------------------------------------------ низкоуровнево
    def _request(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            resp = self._client.get(url, params=params)
            self._last_url = str(resp.request.url)
        except httpx.HTTPError as e:  # сеть/таймаут/DNS — секреты не светим
            raise OdataError(f"соединение с 1С не удалось: {e.__class__.__name__}", code=-1) from e
        if resp.status_code >= 400:
            snippet = (resp.text or "")[:300].replace("\n", " ")
            raise OdataError(
                f"1С ответил HTTP {resp.status_code} на {url.split('?')[0]}: {snippet}",
                code=resp.status_code,
            )
        return resp

    def get_metadata(self) -> bytes:
        """Сырой CSDL $metadata (XML). Пишется в БД как есть (модель ODataMetadata)."""
        return self._request(f"{self.base_url}/$metadata").content

    # ------------------------------------------------------------ коллекции
    @staticmethod
    def _next_link(payload: dict[str, Any]) -> str | None:
        for key in ("odata.nextLink", "@odata.nextLink"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                return v
        return None

    def _entityset_url(self, entity_set: str) -> str:
        # кириллица в имени сущности — обязательный percent-encoding
        return f"{self.base_url}/{quote(entity_set.strip(), safe='')}"

    def iter_collection(
        self,
        entity_set: str,
        *,
        filter_expr: str | None = None,
        select: str | None = None,
        top_limit: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Итератор строк ``value[]`` коллекции со страницами.

        Стратегия: первая страница без токена; дальше, если 1С отдал
        odata.nextLink — идём по нему (разбирая $skiptoken); если nextLink нет,
        но страница полная — продолжаем через $skip/$top (OData v3 1С иногда
        не отдаёт nextLink). Останавливаемся на пустой/неполной странице.
        """
        base = self._entityset_url(entity_set)
        seen_links: set[str] = set()
        yielded = 0
        skip = 0
        url: str | None = base
        params: dict[str, str] = {"$format": "json"}
        if filter_expr:
            params["$filter"] = filter_expr
        if select:
            params["$select"] = select

        for _page_no in range(self.max_pages):
            if url is None:
                break
            p = dict(params)
            p.setdefault("$top", str(self.page_size))
            if skip:
                p["$skip"] = str(skip)

            payload = self._request_json(url, p)
            rows = payload.get("value") or []
            if not isinstance(rows, list):
                raise OdataError(f"неожиданный формат value в ответе для {entity_set}")

            for row in rows:
                if isinstance(row, dict):
                    yield row
                    yielded += 1
                    if top_limit is not None and yielded >= top_limit:
                        return

            nxt = self._next_link(payload)
            if nxt:
                full_url = self._absolutize(nxt)
                if full_url in seen_links:
                    logger.warning("1С замкнул odata.nextLink — остановка (%s)", entity_set)
                    return
                seen_links.add(full_url)
                url = full_url
                params = {"$format": "json"}  # остальные параметры уже в URL
                if filter_expr:
                    # фильтр остаётся актуальным только пока он в URL nextLink;
                    # 1С включает его туда, поэтому повторно не подставляем
                    params.pop("$filter", None)
                continue

            if len(rows) < self.page_size:
                return  # последняя страница
            skip += len(rows)
        else:
            raise OdataError(
                f"превышен лимит страниц ({self.max_pages}) для {entity_set} — прервано"
            )

    def _request_json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        t0 = time.monotonic()
        resp = self._request(url, params)
        try:
            data = resp.json()
        except ValueError as e:
            raise OdataError(f"ответ 1С не является JSON ({url.split('?')[0]})", code=resp.status_code) from e
        logger.debug("GET %s → %d строк за %.2fs", url.split("?")[0],
                     len(data.get("value") or []), time.monotonic() - t0)
        return data

    def _absolutize(self, link: str) -> str:
        """nextLink бывает полным, '/base/...' или '?$skiptoken=...' — сводим к полному URL.

        1С отдаёт nextLink относительно корня OData-сервиса, поэтому query
        склеиваем с URL последней страницы (параметры запроса там уже учтены).
        """
        link = link.strip()
        if link.startswith(("http://", "https://")):
            return link
        last = self._last_url or self.base_url
        if link.startswith("?"):
            return urlsplit(last)._replace(query=urlsplit(link).query).geturl()
        if link.startswith("/"):
            sp = urlsplit(last)
            return urlunsplit((sp.scheme, sp.netloc, link, urlsplit(link).query, ""))
        # относительный без слэша ('Set?$skiptoken=..') — редкость, но бывает
        return f"{self.base_url}/{link.lstrip('./')}"

    # ------------------------------------------------------------ навигация
    def fetch_nav_collection(
        self,
        entity_set: str,
        key_predicate: str,
        nav_property: str,
        *,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """Дочерняя коллекция одной записи: /{Set}({key})/{Nav}.

        key_predicate — «сырая» часть в скобках, напр.
        ``guid'e912e33c-...'`` или ``Recorder='x',LineNumber=1``.
        Используется как fallback, когда пакетный $filter по дочерней таблице
        не принимается 1С (см. worker_sync).
        """
        url = (
            f"{self._entityset_url(entity_set)}"
            f"({quote(key_predicate, safe=\"',=\")})/{quote(nav_property, safe='')}"
        )
        params = {"$format": "json"}
        if filter_expr:
            params["$filter"] = filter_expr
        payload = self._request_json(url, params)
        rows = payload.get("value") or []
        return [r for r in rows if isinstance(r, dict)]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OdataClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
