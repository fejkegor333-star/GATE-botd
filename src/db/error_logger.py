"""
Утилита для записи ошибок в таблицу errors_log
"""
import logging
import traceback
from typing import Optional

from src.db.connection import db
from src.db.models import ErrorLog

logger = logging.getLogger(__name__)


def log_error(
    level: str,
    component: str,
    message: str,
    details: Optional[str] = None,
    symbol: Optional[str] = None,
):
    """
    Записать ошибку в таблицу errors_log

    Args:
        level: Уровень (ERROR, CRITICAL, WARNING)
        component: Компонент (api, trading, ws, telegram, risk)
        message: Сообщение об ошибке
        details: Доп. данные (traceback и т.д.)
        symbol: Символ контракта если ошибка связана с ним
    """
    if not db._initialized:
        return

    try:
        with db.get_session() as session:
            error = ErrorLog(
                level=level,
                component=component,
                message=message[:1000],  # Ограничиваем длину
                details=details[:5000] if details else None,
                symbol=symbol,
            )
            session.add(error)
    except Exception as e:
        logger.debug(f"Не удалось записать ошибку в БД: {e}")


def log_exception(
    component: str,
    message: str,
    exc: Exception,
    symbol: Optional[str] = None,
):
    """Записать исключение с traceback"""
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    log_error(
        level='ERROR',
        component=component,
        message=f"{message}: {exc}",
        details=''.join(tb),
        symbol=symbol,
    )
