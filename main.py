"""
Main entry point для Gate Futures Bot

⚠️ Для тестов можно использовать SQLite: python main.py --sqlite
   Для продакшена: установите PostgreSQL
"""
import asyncio
import logging
import sys
import argparse
import signal
import io

from src.utils.config import config
from src.db.connection import db
from src.bot.core import TradingBot, set_trading_bot

# Sentry для мониторинга ошибок
def setup_sentry():
    """Инициализация Sentry SDK"""
    if not config.sentry_dsn:
        return
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=config.sentry_dsn,
            traces_sample_rate=0.1,
            environment='production' if not config.debug else 'development',
        )
        logging.info("Sentry SDK инициализирован")
    except ImportError:
        logging.warning("sentry-sdk не установлен, Sentry отключен")
    except Exception as e:
        logging.warning(f"Ошибка инициализации Sentry: {e}")


# Настройка логирования
def setup_logging():
    """Настройка логирования"""
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)

    
    if sys.platform == 'win32':
        
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    handlers = [logging.StreamHandler(sys.stdout)]

    
    try:
        import os
        os.makedirs('logs', exist_ok=True)
        handlers.append(logging.FileHandler('logs/bot.log', encoding='utf-8'))
    except Exception as e:
        logging.warning(f"Не удалось создать файл лога: {e}")

    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=handlers,
        force=True  
    )


async def main(use_sqlite: bool = False, enable_telegram: bool = True):
    """Главная функция"""
    
    setup_logging()
    setup_sentry()
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("🚀 Gate Futures Bot запускается...")
    logger.info(f"Версия: 1.0.0")
    logger.info(f"Режим: {'DEBUG' if config.debug else 'PRODUCTION'}")
    if use_sqlite:
        logger.warning("⚠️  БД: SQLite (только для тестов!)")
    if not enable_telegram:
        logger.warning("⚠️  Telegram отключен!")
    logger.info("=" * 60)

    # Обработчики сигналов (не работают на Windows)
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Получен сигнал завершения...")
        shutdown_event.set()

    # Регистрируем обработчики (только для Unix)
    loop = asyncio.get_running_loop()
    if sys.platform != 'win32':
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)

    # Создаем экземпляр бота и регистрируем глобально
    trading_bot = TradingBot(enable_telegram=enable_telegram)
    set_trading_bot(trading_bot)

    try:
        # Валидация конфигурации
        logger.info("Валидация конфигурации...")
        config.validate()

        # Инициализация Redis (опциональный кэш)
        from src.cache.redis_client import redis_cache
        redis_cache.init()

        # Инициализация БД
        logger.info("Инициализация базы данных...")
        db.init_db(use_sqlite=use_sqlite)

        # Запуск торгового бота
        logger.info("Запуск торгового бота...")
        await trading_bot.start()

        logger.info("✅ Бот успешно запущен!")
        logger.info("=" * 60)

        # Ждем сигнал завершения
        # На Windows используем asyncio.sleep вместо signal handlers
        if sys.platform == 'win32':
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
        else:
            await shutdown_event.wait()

    except KeyboardInterrupt:
        logger.info("Получен сигнал KeyboardInterrupt")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Останавливаем бота
        logger.info("Остановка бота...")
        await trading_bot.stop()
        # Закрываем Redis
        try:
            from src.cache.redis_client import redis_cache
            redis_cache.close()
        except Exception:
            pass
        logger.info("✅ Бот остановлен")
        logger.info("=" * 60)


if __name__ == '__main__':
    # Парсер аргументов
    parser = argparse.ArgumentParser(description='Gate Futures Bot')
    parser.add_argument(
        '--sqlite',
        action='store_true',
        help='Использовать SQLite (только для тестов!)'
    )
    parser.add_argument(
        '--tg',
        action='store_true',
        help='Запустить Telegram бота (для отладки)'
    )
    parser.add_argument(
        '--no-telegram',
        action='store_true',
        help='Отключить Telegram бот (для избежания конфликтов)'
    )
    args = parser.parse_args()
    
    # Проверка режима
    if config.debug:
        logging.warning("⚠️  DEBUG РЕЖИМ ВКЛЮЧЕН!")

    if config.dry_run:
        logging.warning("⚠️  DRY RUN РЕЖИМ - ордера не будут исполняться!")

    # Запуск
    try:
        if args.tg:
            # Запуск только Telegram бота для тестов
            from src.telegram.bot import telegram_bot
            import asyncio
            logging.info("Запуск Telegram бота...")
            asyncio.run(telegram_bot.start())
        else:
            # Запуск торгового бота
            asyncio.run(main(use_sqlite=args.sqlite, enable_telegram=not args.no_telegram))
    except KeyboardInterrupt:
        logging.info("Получен сигнал KeyboardInterrupt")
    except Exception as e:
        logging.error(f"Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
