# Развертывание бота на VPS

## Требования к VPS
- Ubuntu 20.04/22.04 или Debian 11+
- Минимум 1GB RAM, 1 CPU
- Python 3.10+

## Установка

### 1. Подключитесь к VPS
```bash
ssh root@your-vps-ip
```

### 2. Установите зависимости
```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git postgresql postgresql-contrib nginx certbot python3-certbot-nginx
```

### 3. Создайте пользователя бота (рекомендуется)
```bash
adduser botuser
usermod -aG sudo botuser
su - botuser
```

### 4. Склонируйте репозиторий
```bash
git clone <your-repo-url> ~/bot
cd ~/bot
```

### 5. Создайте виртуальное окружение
```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 6. Настройте PostgreSQL
```bash
sudo -u postgres psql
```
```sql
CREATE DATABASE gate_bot;
CREATE USER bot_user WITH PASSWORD 'your_strong_password';
GRANT ALL PRIVILEGES ON DATABASE gate_bot TO bot_user;
\q
```

### 7. Настройте .env
```bash
cp .env.example .env
nano .env
```

Обязательно заполните:
```
# База данных PostgreSQL
DB_HOST=localhost
DB_PORT=5432
DB_NAME=gate_bot
DB_USER=bot_user
DB_PASSWORD=your_strong_password

# Gate.io API
GATE_API_KEY=your_api_key
GATE_API_SECRET=your_api_secret
GATE_API_URL=https://api.gateio.ws/api/v4

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_ADMIN_ID=your_telegram_id
```

### 8. Инициализируйте базу данных
```bash
python main.py
```

### 9. Настройте systemd сервис
```bash
sudo nano /etc/systemd/system/gate-bot.service
```

```
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

[Install]
WantedBy=multi-user.target
```

### 10. Запустите бота
```bash
sudo systemctl daemon-reload
sudo systemctl enable gate-bot
sudo systemctl start gate-bot
```

### 11. Проверьте статус
```bash
sudo systemctl status gate-bot
```

### 12. Смотрите логи
```bash
sudo journalctl -u gate-bot -f
```

## Управление

```bash
# Остановить
sudo systemctl stop gate-bot

# Запустить
sudo systemctl start gate-bot

# Перезапустить
sudo systemctl restart gate-bot

# Просмотр логов
sudo journalctl -u gate-bot -f
```

## Обновление

```bash
cd ~/bot
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart gate-bot
```

## Автоматический бэкап (опционально)

```bash
crontab -e
```

Добавьте:
```
0 3 * * * pg_dump -U bot_user gate_bot > ~/backups/bot_$(date +\%Y\%m\%d).sql
```
