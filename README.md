# AI News Aggregator

Самохостящийся агрегатор новостей с Telegram-ботом. Собирает новости по заданной теме из RSS, sitemap, обычных веб-страниц и Telegram-каналов, отбрасывает нерелевантное, группирует дубли в кластеры, оценивает важность и генерирует через LLM русскоязычный черновик новости. Черновик приходит в Telegram карточкой модерации: одобрить, отклонить, перегенерировать или перегенерировать с комментарием. Одобренный текст забирается командой `/approved` и публикуется вручную — автопостинга на сайт нет, это осознанное решение.

Подходит медиа, Telegram-каналам и командам, которым нужен постоянный поток черновиков новостей по своей теме на модерацию, а не готовая автоматическая публикация. Один инстанс обслуживает несколько независимых «воркспейсов»: воркспейс — это Telegram-группа со своей темой, своими источниками, своим фильтром и своим OpenRouter-ключом (BYOK).

## Как это работает

```
Источники (RSS / sitemap / web / Telegram-каналы через RSS-мост)
  → фильтр релевантности по ключевым словам
  → сохранение сырых статей в PostgreSQL
  → дедупликация и кластеризация похожих новостей
  → скоринг (правила + AI-классификация)
  → генерация русскоязычного черновика через LLM
  → карточка модерации в Telegram (approve / reject / regenerate / edit)
  → одобренный текст выдаётся по /approved для ручной публикации
```

Парсинг запускается по расписанию (APScheduler, по умолчанию каждые 15 минут). Кластер живёт по lifecycle `new → waiting → generating → pending_review → approved/rejected`.

## Быстрый старт (Docker)

Понадобятся Docker с Compose, токен бота от [@BotFather](https://t.me/BotFather) и ключ [OpenRouter](https://openrouter.ai/).

```bash
git clone <repo-url> && cd <repo>
cp .env.example .env
```

В `.env` заполнить минимум:

- `BOT_TOKEN` — токен бота от @BotFather;
- `ADMIN_USER_IDS` — ваш Telegram user ID в виде JSON-списка, например `[123456789]`;
- `POSTGRES_PASSWORD` — пароль БД (production-compose не стартует без явного пароля);
- `OPENROUTER_API_KEY` — ключ для генерации в основном воркспейсе.

`POSTGRES_HOST`/`REDIS_HOST` внутри Docker подставляются автоматически (`postgres`/`redis`), менять их не нужно. Затем:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
# или: make prod-up
```

Поднимутся PostgreSQL 16, Redis 7 и контейнер бота. Миграции схемы применяются автоматически при старте приложения (`scripts/migrations/*.sql`, ledger в таблице `schema_migrations`). Проверить, что всё живо: `make prod-logs` и команда `/health` боту в личку.

## Подключение клиентского воркспейса

Воркспейс = Telegram-группа. Каждый воркспейс работает на собственном OpenRouter-ключе владельца (BYOK): без ключа генерация в воркспейсе не запускается, чтобы клиенты не тратили кредиты владельца инстанса.

1. Добавьте бота в группу команды/клиента.
2. Админ группы вызывает в группе `/setup` — создаётся воркспейс.
3. `/set_topic Новости финтеха` — тема воркспейса.
4. Опционально `/set_keywords финтех, банки, платежи` — свой фильтр релевантности вместо встроенного набора ключевых слов.
5. Владелец воркспейса пишет боту **в личку** `/connect_openrouter`, выбирает воркспейс и присылает свой ключ `sk-or-...`. Сообщение с ключом бот удаляет из чата.
6. Источники добавляются в группе через `/add_source` (доступно админам группы).

Карточки модерации приходят прямо в группу воркспейса. Личка владельца инстанса с ботом — «основной» воркспейс с классическим поведением: роли admin/moderator/viewer, карточки в личку, глобальная цепочка AI-провайдеров.

## Команды бота

Личка владельца инстанса (основной воркспейс):

| Команда | Кто | Что делает |
|---|---|---|
| `/start`, `/help` | все | справка по ролям |
| `/approved` | все роли | одобренные статьи за 7 дней, текст удобно копировать |
| `/queue` | все роли | очередь кластеров и черновиков на ревью |
| `/stats` | все роли | статистика пайплайна |
| `/health` | admin | состояние системы: scheduler, БД, Redis, Telegram API, ошибки источников |
| `/add_source`, `/sources` | admin | добавить источник / список с вкл-выкл-удалением |
| `/set_threshold N` | admin | порог скоринга для генерации |
| `/set_prompt` | admin | редакционный промпт генерации |
| `/add_user ID РОЛЬ` | admin | выдать пользователю роль admin/moderator/viewer |
| `/connect_openrouter` | владелец воркспейсов | привязать OpenRouter-ключ к своему воркспейсу |

Группа-воркспейс:

| Команда | Кто | Что делает |
|---|---|---|
| `/setup` | админ группы | создать воркспейс |
| `/workspace` | все в группе | статус и настройки воркспейса |
| `/set_topic ТЕКСТ` | админ группы | тема воркспейса |
| `/set_keywords слова, через запятую` | админ группы | свой фильтр релевантности |
| `/set_prompt` | админ группы | редакционный промпт воркспейса |
| `/add_source`, `/sources` | админ группы / все | управление источниками |
| `/queue`, `/approved`, `/stats` | участники группы | очередь, одобренное, статистика |

Кнопки на карточке модерации: одобрить, отклонить, перегенерировать, перегенерировать с комментарием (edit).

## Конфигурация

Ключевые переменные из `.env.example`:

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | токен Telegram-бота |
| `ADMIN_USER_IDS` | bootstrap-админы (JSON-список Telegram ID); их нельзя разжаловать через `/add_user` |
| `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | подключение к PostgreSQL |
| `REDIS_HOST/PORT` | Redis для FSM-состояний бота |
| `OPENROUTER_API_KEY` | ключ OpenRouter для основного воркспейса |
| `OPENROUTER_SCORING_MODEL`, `OPENROUTER_GENERATION_MODEL` | модели для AI-скоринга и генерации |
| `LOCAL_AI_PROVIDER_ENABLED` | dev-провайдер с детерминированными черновиками, без внешних вызовов (в проде — `false`) |
| `CODEX_PROVIDER_ENABLED`, `CODEX_BIN`, `CODEX_MODEL`, `CODEX_SANDBOX` | опциональный провайдер через Codex CLI (официальный логин ChatGPT, токены хранит сам Codex) |
| `OAUTH_AI_BASE_URL`, `OAUTH_AI_ACCESS_TOKEN`, `OAUTH_AI_TOKEN_FILE`, ... | опциональный OAuth-шлюз с chat-completions API, пробуется до OpenRouter |
| `SCHEDULER_ENABLED`, `PARSING_INTERVAL_MINUTES` | планировщик и интервал парсинга |
| `REQUEST_TIMEOUT_SECONDS` | таймаут HTTP-запросов парсеров |
| `TELEGRAM_RSS_SERVICE` | RSS-мост для чтения Telegram-каналов как фидов |
| `LOG_LEVEL`, `LOG_FORMAT` | логирование (`pretty` для dev, `json` для prod) |

Цепочка провайдеров основного воркспейса: LocalTest (dev) → Codex CLI (опционально) → OAuth-gateway (опционально) → OpenRouter. Клиентские воркспейсы генерируют только через собственный OpenRouter-ключ. Достаточно настроить хотя бы один провайдер.

Полный список переменных, настройка Codex-провайдера в Docker, миграции, healthcheck и порядок деплоя — в [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Локальная разработка

Нужны Python 3.11 и Docker (для Postgres/Redis).

```bash
make up                       # dev Postgres/Redis с открытыми на хост портами
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # BOT_TOKEN обязателен, хосты localhost уже стоят

make run                      # бот в текущем терминале
# либо в фоне: make local-bot-start / local-bot-logs / local-bot-stop
```

Тесты:

```bash
.venv/bin/python -m pytest
```

Для смоук-теста без реальных AI-ключей включите в `.env` `LOCAL_AI_PROVIDER_ENABLED=true` — генерация пойдёт через детерминированный локальный провайдер. Полезные помощники: `make local-check` (проверка готовности окружения) и `make local-test-draft` (отправляет тестовую карточку модерации).

## Архитектура

```
app/
  main.py                 # startup: миграции, роутеры, middlewares, scheduler
  config.py               # Pydantic settings, валидация провайдеров
  database/
    models.py             # ORM: sources, raw_articles, news_clusters,
                          # generated_articles, bot_users, workspaces, settings
    migrations.py         # раннер SQL-миграций (scripts/migrations/*.sql)
  parsers/                # rss (+telegram через RSS-мост), sitemap, web_scraper
  services/
    parsing_cycle.py      # оркестрация цикла: источники → ingest → кластеры → генерация
    ingestion.py          # парсинг источников, фильтр, сохранение raw articles
    filter.py             # keyword-фильтр релевантности (уровень 1, без AI)
    dedup.py              # дедупликация/кластеризация (rapidfuzz + entity matching)
    scoring.py            # скоринг кластеров: правила + AI-классификация
    cluster_pipeline.py   # lifecycle кластеров
    generation_pipeline.py# выбор кластеров и запуск генерации
    content_generator.py  # промпт + парсинг ответа LLM
    ai_providers.py       # цепочка провайдеров, BYOK для воркспейсов
    notifier.py           # карточки модерации, безопасный Telegram HTML
    cleanup.py            # удаление устаревших кластеров/статей
    health.py             # общий health-репорт для /health и Docker healthcheck
    workspaces.py         # резолв воркспейса по чату, права
  handlers/               # Telegram-команды и callbacks (start, sources, settings,
                          # moderation, viewer, workspace)
  keyboards/, middlewares/, states/, utils/
scripts/migrations/       # SQL-миграции схемы, применяются при старте
prompts/                  # дефолтный редакционный промпт
tests/                    # pytest-suite: пайплайн, handlers, миграции, health
```

## Точки расширения

Что напрашивается достроить поверх текущей базы:

- **Веб-кабинет** — управление источниками, промптом и очередью через браузер вместо команд бота.
- **Биллинг и метеринг** — учёт токенов/генераций по воркспейсам, лимиты, тарифы.
- **Авто-генерация редполитики** — строить промпт воркспейса из примеров текстов клиента, а не редактировать руками через `/set_prompt`.
- **LLM-фильтр релевантности** — заменить или дополнить keyword-фильтр (`app/services/filter.py`) дешёвой моделью-классификатором.
- **Вынос парсинга в воркеры** — сейчас парсинг живёт в процессе бота на APScheduler; для большого числа источников его можно вынести в отдельные воркеры с очередью.

## Лицензия

MIT — см. файл [LICENSE](LICENSE).
