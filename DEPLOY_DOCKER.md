# Деплой Gate Futures Bot в Docker

## Быстрый старт

```bash
# 1. Клонировать репозиторий
git clone <repo-url> gate-bot
cd gate-bot

# 2. Создать .env из шаблона и заполнить
cp .env.example .env
nano .env

# 3. Запустить автодеплой
sudo bash deploy.sh
```

Скрипт `deploy.sh` сам проверит системные требования, установит Docker при необходимости, соберёт образы, инициализирует БД и запустит бота.

---

## Системные требования

| Параметр | Минимум |
|----------|---------|
| ОС | Ubuntu 20.04+ / Debian 11+ |
| RAM | 1 GB |
| Диск | 5 GB свободно |
| Docker | Устанавливается автоматически |
| Docker Compose v2 | Устанавливается автоматически |

## Структура деплоя

```
gate-bot/
├── docker-compose.yml   # Описание сервисов (бот, postgres, redis)
├── Dockerfile           # Сборка образа бота
├── deploy.sh            # Скрипт автоматического деплоя
├── .env                 # Конфигурация (создать из .env.example)
├── logs/                # Логи бота (создаётся автоматически)
│   └── bot.log
└── data/
    └── postgres/        # Данные PostgreSQL (создаётся автоматически)
```

## Заполнение .env

Скопируйте `.env.example` в `.env` и заполните **обязательные** параметры:

| Параметр | Описание | Обязательный |
|----------|---------|:---:|
| `GATE_API_KEY` | API-ключ Gate.io | да |
| `GATE_API_SECRET` | API-секрет Gate.io | да |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота (от @BotFather) | да |
| `TELEGRAM_ADMIN_ID` | Ваш Telegram ID (число) | да |
| `DB_PASSWORD` | Пароль PostgreSQL (генерируется автоматически если пустой) | нет |
| `SENTRY_DSN` | DSN для Sentry мониторинга | нет |
| `DRY_RUN` | `true` — тестовый режим без реальных ордеров | нет |
| `DEBUG` | `true` — расширенное логирование | нет |

**Параметры БД и Redis** (`DB_HOST`, `DB_PORT`, `REDIS_HOST`, `REDIS_PORT`) задаются автоматически через docker-compose и в `.env` менять их **не нужно**.

## Сервисы

| Контейнер | Образ | Назначение | Порты наружу |
|-----------|-------|-----------|:---:|
| `gate-bot` | Python 3.11 (собирается) | Торговый бот | нет |
| `gate-postgres` | postgres:17-alpine | База данных | нет |
| `gate-redis` | redis:7-alpine | Кэш | нет |

> Все контейнеры общаются только внутри Docker-сети. Наружу порты не проброшены — это безопаснее.

## Управление

```bash
# Статус контейнеров
docker compose ps

# Логи бота (файл)
tail -f logs/bot.log

# Логи бота (docker)
docker logs -f gate-bot

# Перезапуск бота
docker compose restart bot

# Остановить всё
docker compose down

# Остановить и удалить данные БД (ОСТОРОЖНО!)
docker compose down -v
```

## Обновление

```bash
git pull
docker compose build bot
docker compose up -d bot
```

## Бэкап базы данных

```bash
# Ручной бэкап
docker exec gate-postgres pg_dump -U gate_bot gate_bot > backup_20260323.sql

# Восстановление
cat backup_YYYYMMDD.sql | docker exec -i gate-postgres psql -U gate_bot gate_bot
```

## Что делает deploy.sh

1. **Проверяет систему** — ОС, RAM (≥1 GB), диск (≥5 GB)
2. **Устанавливает Docker** и Docker Compose v2 если не найдены
3. **Создаёт директории** — `logs/`, `data/postgres/`
4. **Проверяет .env** — все обязательные параметры заполнены, генерирует DB_PASSWORD если пустой
5. **Собирает образы** — скачивает postgres/redis, собирает образ бота
6. **Инициализирует БД** — создаёт таблицы и настройки по умолчанию
7. **Запускает бота** — проверяет что контейнер стартовал
8. **Выводит отчёт** — статус контейнеров и полезные команды

## Решение проблем

**Бот не стартует:**
```bash
docker logs gate-bot --tail 50
```

**PostgreSQL не стартует:**
```bash
docker logs gate-postgres --tail 50
# Возможно повреждены данные — удалите и переинициализируйте:
docker compose down
rm -rf data/postgres
bash deploy.sh
```

**Переинициализация БД (сброс всех данных):**
```bash
docker compose down
rm -rf data/postgres
docker compose up -d postgres redis
sleep 5
docker compose run --rm bot python init_db.py
docker compose up -d bot
```
