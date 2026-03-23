"""
Утилита для ведения истории P&L по дням
"""
import logging
from datetime import datetime
from typing import Optional

from src.db.connection import db
from src.db.models import PnlHistory, Trade

logger = logging.getLogger(__name__)


def update_daily_pnl(
    pnl: float,
    volume_usdt: float = 0,
    is_winning: Optional[bool] = None,
):
    """
    Обновить PnL за текущий день

    Args:
        pnl: PnL сделки
        volume_usdt: Объём сделки
        is_winning: True если прибыльная, False если убыточная, None если не сделка
    """
    if not db._initialized:
        return

    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        with db.get_session() as session:
            record = session.query(PnlHistory).filter(
                PnlHistory.date == today_start
            ).first()

            if not record:
                record = PnlHistory(
                    date=today_start,
                    realized_pnl=0,
                    total_trades=0,
                    winning_trades=0,
                    losing_trades=0,
                    total_volume_usdt=0,
                )
                session.add(record)

            record.realized_pnl = float(record.realized_pnl or 0) + pnl
            record.total_volume_usdt = float(record.total_volume_usdt or 0) + volume_usdt

            if is_winning is True:
                record.total_trades = (record.total_trades or 0) + 1
                record.winning_trades = (record.winning_trades or 0) + 1
            elif is_winning is False:
                record.total_trades = (record.total_trades or 0) + 1
                record.losing_trades = (record.losing_trades or 0) + 1

    except Exception as e:
        logger.debug(f"Не удалось обновить PnL history: {e}")


def save_daily_balance(balance_start: Optional[float] = None, balance_end: Optional[float] = None):
    """Сохранить баланс на начало/конец дня"""
    if not db._initialized:
        return

    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        with db.get_session() as session:
            record = session.query(PnlHistory).filter(
                PnlHistory.date == today_start
            ).first()

            if not record:
                record = PnlHistory(date=today_start)
                session.add(record)

            if balance_start is not None:
                record.balance_start = balance_start
            if balance_end is not None:
                record.balance_end = balance_end

    except Exception as e:
        logger.debug(f"Не удалось сохранить баланс: {e}")
