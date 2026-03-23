@echo off
REM Скрипт запуска бота для Windows
REM Устанавливает переменную окружения и запускает бота с SQLite

echo ============================================================
echo Gate Futures Bot - Запуск
echo ============================================================

REM Устанавливаем переменную окружения для текущей сессии
set GATE_API_URL=https://api.gateio.ws/api/v4

REM Запускаем бота с SQLite (для тестов)
REM Для продакшена используйте: python main.py (без --sqlite)
python main.py --sqlite

pause
