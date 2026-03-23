"""
Модуль подключения к базе данных
Поддерживает PostgreSQL (продакшен) и SQLite (тесты)
"""
import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, Pool, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool, StaticPool

from src.db.models import Base
from src.utils.config import config

logger = logging.getLogger(__name__)


class Database:
    """Класс для работы с базой данных"""

    def __init__(self):
        self.engine = None
        self.SessionLocal = None
        self._initialized = False
        self._use_sqlite = False

    def init_db(self, use_sqlite: bool = False):
        """
        Инициализация подключения к БД

        Args:
            use_sqlite: Если True - использовать SQLite (ДЛЯ ТЕСТОВ!)
        """
        if self._initialized:
            return

        self._use_sqlite = use_sqlite

        if use_sqlite:
            # ⚠️ SQLite для тестов - НЕ ДЛЯ ПРОДАКШЕНА!
            logger.warning("=" * 60)
            logger.warning("⚠️  ВНИМАНИЕ: ИСПОЛЬЗУЕТСЯ SQLite (только для тестов!)")
            logger.warning("⚠️  Для продакшена необходимо установить PostgreSQL!")
            logger.warning("⚠️  См. README.md для инструкций по установке")
            logger.warning("=" * 60)
            db_url = "sqlite:///gate_bot.db"
            
            # SQLite не поддерживает пулы соединений
            self.engine = create_engine(
                db_url,
                connect_args={"check_same_thread": False},
                echo=config.debug,
            )
        else:
            # PostgreSQL для продакшена
            db_url = (
                f"postgresql://{config.db.user}:{config.db.password}"
                f"@{config.db.host}:{config.db.port}/{config.db.name}"
            )

            self.engine = create_engine(
                db_url,
                poolclass=QueuePool,
                pool_size=config.db.pool_size,
                max_overflow=config.db.max_overflow,
                pool_recycle=config.db.pool_recycle,
                echo=config.debug,
                pool_pre_ping=True,
            )

        # Создаем фабрику сессий
        self.SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=self.engine
        )

        self._initialized = True

        if use_sqlite:
            # Для SQLite автоматически создаём таблицы
            Base.metadata.create_all(self.engine)
            logger.info("✅ SQLite инициализирован (gate_bot.db)")
        else:
            logger.info(f"✅ PostgreSQL инициализирован: {config.db.host}:{config.db.port}/{config.db.name}")

    def create_tables(self):
        """Создание всех таблиц"""
        if not self._initialized:
            self.init_db()

        Base.metadata.create_all(self.engine)
        logger.info("Таблицы базы данных созданы")

    def drop_tables(self):
        """Удаление всех таблиц (ОСТОРОЖНО!)"""
        if not self._initialized:
            self.init_db()

        Base.metadata.drop_all(self.engine)
        logger.warning("Все таблицы удалены")

    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """
        Получить сессию для работы с БД

        Использование:
            with db.get_session() as session:
                contracts = session.query(Contract).all()
        """
        if not self._initialized:
            self.init_db()

        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Ошибка базы данных: {e}")
            raise
        finally:
            session.close()

    def get_session_sync(self) -> Session:
        """
        Получить сессию (синхронная версия)

        ВНИМАНИЕ: После использования нужно закрыть сессию вручную:
            session = db.get_session_sync()
            try:
                # работа с БД
                pass
            finally:
                session.close()
        """
        if not self._initialized:
            self.init_db()

        return self.SessionLocal()


# Глобальный инстанс
db = Database()


async def init_db():
    """Асинхронная инициализация БД"""
    db.init_db()
    logger.info("База данных инициализирована")


async def get_db_session() -> Generator[Session, None, None]:
    """
    Dependency injection для FastAPI или подобных фреймворков

    Использование:
        @app.get("/contracts")
        async def get_contracts(session: Session = Depends(get_db_session)):
            return session.query(Contract).all()
    """
    with db.get_session() as session:
        yield session
