#!/bin/bash
# Скрипт быстрой установки Gate Futures Bot на VPS (Ubuntu/Debian)

set -e

echo "=== Установка Gate Futures Bot на VPS ==="

# Проверка root
if [ "$EUID" -ne 0 ]; then
    echo "Запустите скрипт с sudo: sudo ./install.sh"
    exit 1
fi

# Создание пользователя
echo "1. Создание пользователя botuser..."
if ! id "botuser" &>/dev/null; then
    adduser --gecos "" --disabled-password botuser
    usermod -aG sudo botuser
    echo "botuser:secure_password" | chpasswd
fi

# Установка зависимостей
echo "2. Установка зависимостей..."
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git postgresql postgresql-contrib

# Настройка PostgreSQL
echo "3. Настройка PostgreSQL..."
sudo -u postgres psql -c "CREATE DATABASE gate_bot;" || echo "БД уже существует"
sudo -u postgres psql -c "CREATE USER bot_user WITH PASSWORD 'gate_bot_password_123';" || echo "Пользователь уже существует"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE gate_bot TO bot_user;"
sudo -u postgres psql -c "ALTER USER bot_user WITH PASSWORD 'gate_bot_password_123';"

# Создание директории бота
echo "4. Создание директории бота..."
mkdir -p /home/botuser/bot
chown -R botuser:botuser /home/botuser/bot

# Копирование файлов (если запускаем из папки бота)
if [ -f "main.py" ]; then
    echo "5. Копирование файлов бота..."
    cp -r . /home/botuser/bot/
    chown -R botuser:botuser /home/botuser/bot
else
    echo "5. Пропуск копирования (запустите из папки бота или клонируйте репозиторий)"
fi

# Создание виртуального окружения
echo "6. Создание виртуального окружения..."
sudo -u botuser python3 -m venv /home/botuser/bot/venv
sudo -u botuser /home/botuser/bot/venv/bin/pip install --upgrade pip

# Установка зависимостей Python
if [ -f "/home/botuser/bot/requirements.txt" ]; then
    echo "7. Установка Python зависимостей..."
    sudo -u botuser /home/botuser/bot/venv/bin/pip install -r /home/botuser/bot/requirements.txt
fi

# Создание .env файла
if [ ! -f "/home/botuser/bot/.env" ]; then
    echo "8. Создание .env файла..."
    cat > /home/botuser/bot/.env << 'EOF'
# Database
DB_HOST=localhost
DB_PORT=5432
DB_NAME=gate_bot
DB_USER=bot_user
DB_PASSWORD=gate_bot_password_123

# Gate.io API (заполните своими ключами)
GATE_API_KEY=your_api_key_here
GATE_API_SECRET=your_api_secret_here
GATE_API_URL=https://api.gateio.ws/api/v4

# Telegram (заполните своими данными)
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_ADMIN_ID=your_telegram_id_here

# Settings
LOG_LEVEL=INFO
DEBUG=false
DRY_RUN=false
EOF
    echo "⚠️  ОТРЕДАКТИРУЙТЕ /home/botuser/bot/.env своими данными!"
fi

# Создание systemd сервиса
echo "9. Создание systemd сервиса..."
cat > /etc/systemd/system/gate-bot.service << 'EOF'
[Unit]
Description=Gate Futures Bot
After=network.target postgresql.service

[Service]
Type=simple
User=botuser
WorkingDirectory=/home/botuser/bot
Environment="PATH=/home/botuser/bot/venv/bin"
ExecStart=/home/botuser/bot/venv/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable gate-bot

echo ""
echo "=== Установка завершена! ==="
echo ""
echo "Что делать дальше:"
echo "1. Отредактируйте /home/botuser/bot/.env своими ключами API"
echo "2. Запустите: sudo systemctl start gate-bot"
echo "3. Проверьте статус: sudo systemctl status gate-bot"
echo "4. Смотрите логи: sudo journalctl -u gate-bot -f"
echo ""
