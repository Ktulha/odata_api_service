# ics-odata-api

Read-only API-прокси к публикации 1С по OData (`/odata/standard.odata`). Клиенты **не могут**
выполнять произвольные OData-запросы: все данные отдаются из собственного кеша (PostgreSQL/SQLite),
который наполняет отдельный процесс-worker. Доступ — bearer-токены, роли → набор сущностей.

## Архитектура (в двух словах)

| Роль процесса | Что делает | 1С видит? |
|---|---|---|
| `api` | только чтение из БД-кеша, `/api/v1/*` | нет |
| `admin` | настройка ролей/сущностей/расписания, логи | нет |
| `worker` | синхронизация `$metadata` и данных по расписанию/принудительно | **да (единственный)** |

Данные: одна таблица на сущность, имена колонок как в 1С (кириллица ок), ссылки `X_Key -> Ref_Key`.
Загрузка: временная staging-таблица → upsert в боевую. Пустые таблицы-прокладки разрешаются
через алиасы (`@source` в ответах). Разрешения хранятся в БД; запрещённая сущность не кешируется
и не попадает в вывод/связи.

## Требования

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- PostgreSQL — для production; SQLite подходит для проверки подключения к 1С

## Установка

```bash
git clone <URL_вашего_репозитория> odata-cache
cd odata-cache
uv sync                       # создаст .venv и установит зависимости
```

## Конфигурация

Все настройки читаются из переменных окружения с префиксом `ODATA_` и/или файла `.env` в корне:

```bash
cp .env.example .env
```

Минимально для проверки с реальной 1С:

| Переменная | Пример | Кто использует |
|---|---|---|
| `ODATA_DATABASE_URL` | `sqlite:///./odata_cache.db` или `postgresql+psycopg://app:secret@localhost:5432/odata_cache` | все |
| `ODATA_ONEDATA_BASE_URL` | `http://192.168.14.152/unf_14062023/odata/standard.odata` | только worker/probe |
| `ODATA_ONEDATA_USERNAME` | пользователь, под которым опубликована база | только worker/probe |
| `ODATA_ONEDATA_PASSWORD` | пароль публикации | только worker/probe |
| `ODATA_ONEDATA_TIMEOUT` | `30` (сек.) | только worker/probe |

> ВНИМАНИЕ: реквизиты 1С должны быть заданы **только** в окружении worker-процесса.
> В `.env` для api/admin их можно оставить пустыми.

## Шаг 0. Проверка тестов

```bash
uv run pytest            # 65 тестов, сеть не нужна
```

## Шаг 1. Диагностика подключения к 1С (ничего не пишет в БД)

```bash
uv run python scripts/probe_1c.py                      # параметры возьмёт из .env
# или явно:
uv run python scripts/probe_1c.py \
    --base-url "http://192.168.14.152/unf_14062023/odata/standard.odata" \
    --user "sync_user" --password "secret" \
    --entity Catalog_Номенклатура --top 5
# самоподписанный HTTPS:
uv run python scripts/probe_1c.py --insecure ...
```

Скрипт последовательно проверяет: корень публикации → `$metadata` (basic-auth) → разбор
`$metadata` нашим парсером (список EntitySet) → одну страницу данных выбранной сущности.
Если парсер споткнётся на реальном `$metadata` — сохраните вывод и пришлите фрагмент XML.

Полезно сразу сохранить сырой `$metadata` для отладки:

```bash
uv run odata-api sync-metadata --save-file fixtures/metadata.xml   # см. шаг 2
```

## Шаг 2. Инициализация БД и реестра сущностей

```bash
# a) служебные таблицы кеша (реестр, права, токены, логи; dev-вариант без Alembic)
uv run odata-api db init

# b) $metadata из 1С -> реестр (данные НЕ грузятся — только схема)
uv run odata-api sync-metadata
# вывод: Реестр: создано N, обновлено M, ...; связей разрешено: K
```

Для PostgreSQL предварительно создайте БД и роль, например:

```sql
CREATE DATABASE odata_cache;
CREATE USER app WITH PASSWORD 'secret';
GRANT ALL PRIVILEGES ON DATABASE odata_cache TO app;
```

Что появилось в реестре: каждая сущность со статусом `discovered` (не активна). Кеширование
включается либо админом принудительно/по расписанию (`scheduled`), либо лениво при первом
обращении клиента (`on_demand`) — если сущность разрешена её роли.

## Шаг 3. Запуск процессов

```bash
# HTTP API + админка (режим разработки):
uv run odata-api serve --mode all --host 0.0.0.0 --port 8000

# отдельно (production):
uv run odata-api serve --mode api   --host 0.0.0.0 --port 8000
uv run odata-api serve --mode admin --host 127.0.0.1 --port 8001

# worker — планировщик синхронизации (шаг 9, появится позже);
# сейчас загрузка данных доступна через конвейер loader'а и CLI-команды разработки.
```

Проверка:

```bash
curl http://localhost:8000/healthz     # {"mode": "...", "status": "ok"}
```

## Тестирование на сниппетах реальных данных (без сети)

`fixtures/Catalog_Номенклатура.json` — реальный ответ УНФ. Чтобы добавить свой фрагмент:

```bash
# из stdin:
curl -u user:pass "http://host/base/odata/standard.odata/Catalog_X?$top=50&$format=json" \
  | uv run python fixtures/save_sample.py Catalog_X
# или напрямую (чувствительные поля вырезаются):
uv run python fixtures/save_sample.py Catalog_X --url "http://.../Catalog_X?\$top=50&\$format=json"
```

Полный конвейер (metadata → registry → DDL → staging/upsert → фильтр) прогоняется тестами
`tests/test_real_sample.py`.

## Структура проекта

```
src/odata_cache/
  config.py            настройки (pydantic-settings, env ODATA_*)
  app_factory.py       Flask factory, режимы api/admin/all/worker
  cli.py               точка входа odata-api (serve, db init, sync-metadata)
  metadata.py          стрим-парсер CSDL $metadata (+ связи *_Key)
  filter_dsl.py        парсер $filter (sugar ~ = <> + канон eq/in/contains...)
  filter_compile.py    AST фильтра -> SQLAlchemy (параметризованный SQL)
  entity_info.py       описание сущности для клиентов/AI-агентов
  db/                  engine/sessions (PG + SQLite)
  models/              ORM: реестр, алиасы, права, токены, логи, метаданные
  services/            access (права), ddl (таблицы сущностей), load_plan (BFS зависимостей),
                       registry_service ($metadata->реестр), entity_resolution (алиасы/@source)
  worker/loader.py     нормализация строк 1С, staging -> upsert (без httpx)
scripts/probe_1c.py    диагностика подключения к 1С
fixtures/              реальные сниппеты для тестов
docs/                  план, формат фильтров, PR
```

## Текущий статус / чего ещё нет

- ✅ шаги: каркас, модели БД, парсер `$metadata`, реестр+права, план загрузки (BFS без `$expand`),
  разрешение алиасов (`@source`), DSL+компилятор `$filter`, конвейер loader'а, entity info
- ⬜ в работе: HTTP-эндпоинты data-API (`/api/v1/entities`, `/api/v1/<entity>/info`, `/api/v1/data/<entity>`),
  bearer-авторизация, админка, worker+APScheduler, Alembic-миграции
