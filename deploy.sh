#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# Gate Futures Bot — автоматический деплой
# Запуск: bash deploy.sh
# ─────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[!!]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

echo '============================================='
echo ' Gate Futures Bot — Deploy Script'
echo '============================================='
echo

# ── 1. Системные требования ──────────────────────────────────
echo '--- Шаг 1: Проверка системных требований ---'

# OS
if [[ ! -f /etc/os-release ]]; then
  fail 'Не удалось определить ОС. Требуется Linux (Ubuntu/Debian).'
fi
source /etc/os-release
ok "ОС: $PRETTY_NAME"

# RAM (минимум 1 GB)
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_MB=$((TOTAL_RAM_KB / 1024))
if (( TOTAL_RAM_MB < 900 )); then
  fail "Недостаточно RAM: ${TOTAL_RAM_MB} MB (нужно минимум 1024 MB)"
fi
ok "RAM: ${TOTAL_RAM_MB} MB"

# Disk (минимум 5 GB свободно)
FREE_DISK_KB=$(df --output=avail / | tail -1 | tr -d ' ')
FREE_DISK_GB=$((FREE_DISK_KB / 1024 / 1024))
if (( FREE_DISK_GB < 5 )); then
  fail "Недостаточно места на диске: ${FREE_DISK_GB} GB (нужно минимум 5 GB)"
fi
ok "Свободное место: ${FREE_DISK_GB} GB"

# ── 2. Docker и Docker Compose ───────────────────────────────
echo
echo '--- Шаг 2: Docker и Docker Compose ---'

if ! command -v docker &>/dev/null; then
  warn 'Docker не найден. Устанавливаю...'
  apt-get update -qq
  apt-get install -y -qq docker.io docker-compose-v2 >/dev/null 2>&1     || curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
  ok 'Docker установлен'
else
  ok "Docker: $(docker --version | head -1)"
fi

if ! docker compose version &>/dev/null; then
  warn 'Docker Compose v2 не найден. Устанавливаю...'
  apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1     || {
      mkdir -p /usr/local/lib/docker/cli-plugins
      curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)         -o /usr/local/lib/docker/cli-plugins/docker-compose
      chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
    }
  ok 'Docker Compose v2 установлен'
else
  ok "Docker Compose: $(docker compose version --short)"
fi

# Проверка что Docker daemon запущен
if ! docker info &>/dev/null; then
  fail 'Docker daemon не запущен. Выполните: systemctl start docker'
fi
ok 'Docker daemon работает'

# ── 3. Структура папок и .env ────────────────────────────────
echo
echo '--- Шаг 3: Подготовка окружения ---'

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"
ok "Рабочая директория: $SCRIPT_DIR"

mkdir -p logs data/postgres
ok 'Директории logs/ и data/postgres/ созданы'

# Создать .env из шаблона если отсутствует
if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    warn '.env создан из .env.example — ОБЯЗАТЕЛЬНО заполните его!'
    warn 'Откройте .env в редакторе: nano .env'
    echo
    fail 'Заполните .env и запустите скрипт повторно.'
  else
    fail '.env.example не найден. Невозможно продолжить.'
  fi
fi
ok '.env файл найден'

# ── 4. Валидация .env ────────────────────────────────────────
echo
echo '--- Шаг 4: Проверка .env ---'

ENV_ERRORS=0

check_env() {
  local key=$1
  local val
  val=$(grep "^${key}=" .env 2>/dev/null | cut -d'=' -f2- | tr -d '"' | tr -d "'")
  if [[ -z "$val" || "$val" == "your_"* || "$val" == "PLACEHOLDER" || "$val" == "0" && "$key" == "TELEGRAM_ADMIN_ID" ]]; then
    warn "$key — не заполнен!"
    ENV_ERRORS=$((ENV_ERRORS + 1))
  else
    ok "$key — установлен"
  fi
}

check_env GATE_API_KEY
check_env GATE_API_SECRET
check_env TELEGRAM_BOT_TOKEN
check_env TELEGRAM_ADMIN_ID
check_env DB_PASSWORD

# DB_HOST/DB_PORT — подставляются автоматически docker compose, но нужны в .env
# Убедимся что DB_PASSWORD есть
DB_PASS=$(grep '^DB_PASSWORD=' .env 2>/dev/null | cut -d'=' -f2-)
if [[ -z "$DB_PASS" || "$DB_PASS" == "your_"* ]]; then
  # Генерируем пароль автоматически
  GENERATED_PASS=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | head -c 16)
  sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=${GENERATED_PASS}|" .env
  ok "DB_PASSWORD — сгенерирован автоматически"
  ENV_ERRORS=$((ENV_ERRORS > 0 ? ENV_ERRORS - 1 : 0))
fi

if (( ENV_ERRORS > 0 )); then
  echo
  fail "В .env не заполнено ${ENV_ERRORS} обязательных параметров. Заполните и запустите повторно."
fi

ok 'Все обязательные параметры .env заполнены'

# ── 5. Сборка и запуск ───────────────────────────────────────
echo
echo '--- Шаг 5: Сборка и запуск ---'

echo 'Скачиваю образы...'
docker compose pull postgres redis 2>&1 | tail -2
ok 'Образы postgres и redis скачаны'

echo 'Собираю образ бота...'
docker compose build bot 2>&1 | tail -3
ok 'Образ бота собран'

echo 'Запускаю postgres и redis...'
docker compose up -d postgres redis 2>&1 | tail -3
sleep 5

# Проверка healthcheck
if ! docker inspect gate-postgres --format='{{.State.Health.Status}}' 2>/dev/null | grep -q healthy; then
  fail 'PostgreSQL не стартовал (healthcheck failed)'
fi
ok 'PostgreSQL запущен и healthy'

if ! docker inspect gate-redis --format='{{.State.Health.Status}}' 2>/dev/null | grep -q healthy; then
  fail 'Redis не стартовал (healthcheck failed)'
fi
ok 'Redis запущен и healthy'

# ── 6. Инициализация БД ─────────────────────────────────────
echo
echo '--- Шаг 6: Инициализация базы данных ---'

docker compose run --rm bot python init_db.py 2>&1 | grep -E '(INFO|ERROR|WARNING)' | tail -5
if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
  fail 'Ошибка при инициализации БД. Проверьте логи выше.'
fi
ok 'База данных инициализирована'

# ── 7. Запуск бота ───────────────────────────────────────────
echo
echo '--- Шаг 7: Запуск бота ---'

docker compose up -d bot 2>&1 | tail -2
sleep 3

BOT_STATUS=$(docker inspect gate-bot --format='{{.State.Status}}' 2>/dev/null || echo 'not found')
if [[ "$BOT_STATUS" != 'running' ]]; then
  warn "Бот не запущен (статус: $BOT_STATUS). Логи:"
  docker logs gate-bot --tail 20 2>&1
  fail 'Бот не стартовал. Проверьте .env и логи.'
fi
ok 'Бот запущен'

# ── Отчёт ────────────────────────────────────────────────────
echo
echo '============================================='
echo -e "${GREEN} Деплой завершён успешно!${NC}"
echo '============================================='
echo
echo 'Контейнеры:'
docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
echo
echo 'Полезные команды:'
echo "  Логи бота:      tail -f $(pwd)/logs/bot.log"
echo "  Логи (docker):  docker logs -f gate-bot"
echo '  Статус:         docker compose ps'
echo '  Перезапуск:     docker compose restart bot'
echo '  Остановка:      docker compose down'
echo "  Обновление:     git pull && docker compose build bot && docker compose up -d bot"
echo
