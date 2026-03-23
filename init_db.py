"""
Скрипт инициализации базы данных
Создает таблицы и начальные настройки

⚠️ Для тестов используется SQLite (не требует PostgreSQL)
   Для продакшена: установите PostgreSQL и используйте use_sqlite=False
"""
import sys
import logging
import argparse

# Добавляем корневую директорию в path
sys.path.insert(0, '.')

from src.db.connection import db
from src.db.settings import SettingsManager
from src.utils.config import config

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


def main():
    """Главная функция"""
    # Парсер аргументов
    parser = argparse.ArgumentParser(description='Инициализация базы данных')
    parser.add_argument(
        '--sqlite', 
        action='store_true',
        help='Использовать SQLite (только для тестов!)'
    )
    args = parser.parse_args()
    
    try:
        # Валидация конфигурации
        logger.info("Валидация конфигурации...")
        config.validate()

        # Инициализация БД
        logger.info("Инициализация подключения к БД...")
        db.init_db(use_sqlite=args.sqlite)

        # Создание таблиц
        logger.info("Создание таблиц...")
        db.create_tables()

        # Инициализация настроек
        logger.info("Инициализация настроек...")
        with db.get_session() as session:
            settings_manager = SettingsManager(session)
            settings_manager.init_default_settings()

        # Проверка
        with db.get_session() as session:
            all_settings = SettingsManager(session).get_all()
            logger.info(f"Загружено настроек: {len(all_settings)}")
            for name, value in all_settings.items():
                logger.info(f"  {name} = {value}")

        if args.sqlite:
            logger.info("=" * 60)
            logger.info("✅ База данных SQLite успешно инициализирована!")
            logger.info("⚠️  ВНИМАНИЕ: Это только для тестов!")
            logger.info("⚠️  Для продакшена установите PostgreSQL")
            logger.info("=" * 60)
        else:
            logger.info("✅ База данных успешно инициализирована!")

    except Exception as e:
        logger.error(f"❌ Ошибка при инициализации БД: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
