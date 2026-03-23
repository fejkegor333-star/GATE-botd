"""
SQLAlchemy модели базы данных
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DECIMAL, Boolean, DateTime, Text, Enum, ForeignKey, Index
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Contract(Base):
    """Таблица контрактов"""
    __tablename__ = 'contracts'

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), unique=True, nullable=False, index=True)
    launch_time = Column(DateTime, nullable=False, index=True)
    status = Column(String(20), default='new', nullable=False, index=True)  # new, in_work, completed
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    ath_price = Column(DECIMAL(20, 8))
    ath_updated_at = Column(DateTime)
    listing_taken_in_work = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Отношения
    positions = relationship("Position", back_populates="contract")

    def __repr__(self):
        return f"<Contract(symbol={self.symbol}, status={self.status})>"


class Position(Base):
    """Таблица позиций"""
    __tablename__ = 'positions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    contract_symbol = Column(String(50), ForeignKey('contracts.symbol'), nullable=False, index=True)
    entry_price = Column(DECIMAL(20, 8), nullable=False)  # Средняя цена (обновляется при усреднении)
    initial_entry_price = Column(DECIMAL(20, 8), nullable=False)  # Начальная цена входа (не меняется)
    current_price = Column(DECIMAL(20, 8))
    total_volume_usdt = Column(DECIMAL(20, 8), nullable=False)
    avg_count = Column(Integer, default=0)
    unrealized_pnl = Column(DECIMAL(20, 8))
    status = Column(String(20), default='open', nullable=False, index=True)  # open, closed
    days_since_listing = Column(Integer, default=0)
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Отношения
    contract = relationship("Contract", back_populates="positions")
    averaging_history = relationship("AveragingHistory", back_populates="position")

    def __repr__(self):
        return f"<Position(symbol={self.contract_symbol}, status={self.status})>"


class AveragingHistory(Base):
    """История усреднений"""
    __tablename__ = 'averaging_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey('positions.id'), nullable=False, index=True)
    contract_symbol = Column(String(50), nullable=False, index=True)
    avg_number = Column(Integer, nullable=False)  # 1, 2, 3...
    avg_level_pct = Column(DECIMAL(10, 2), nullable=False)  # 300, 700, 1000
    avg_amount_usdt = Column(DECIMAL(20, 8), nullable=False)
    avg_price = Column(DECIMAL(20, 8), nullable=False)
    avg_entry_price = Column(DECIMAL(20, 8), nullable=False)  # Средняя цена после усреднения
    created_at = Column(DateTime, default=datetime.utcnow)

    # Отношения
    position = relationship("Position", back_populates="averaging_history")

    def __repr__(self):
        return f"<AveragingHistory(avg_number={self.avg_number}, level={self.avg_level_pct}%)"


class Trade(Base):
    """История торговых операций"""
    __tablename__ = 'trades'

    id = Column(Integer, primary_key=True, autoincrement=True)
    contract_symbol = Column(String(50), nullable=False, index=True)
    trade_type = Column(String(20), nullable=False, index=True)  # open, close, avg_open, tp_close
    price = Column(DECIMAL(20, 8), nullable=False)
    volume_usdt = Column(DECIMAL(20, 8), nullable=False)
    pnl = Column(DECIMAL(20, 8))
    fee = Column(DECIMAL(20, 8))
    order_id = Column(String(100))
    created_at = Column(DateTime, nullable=False, index=True, default=datetime.utcnow)

    def __repr__(self):
        return f"<Trade(symbol={self.contract_symbol}, type={self.trade_type}, pnl={self.pnl})>"


class Setting(Base):
    """Настройки бота"""
    __tablename__ = 'settings'

    id = Column(Integer, primary_key=True, autoincrement=True)
    param_name = Column(String(50), unique=True, nullable=False, index=True)
    param_value = Column(Text, nullable=False)
    param_type = Column(String(10), nullable=False)  # int, float, str, json, bool
    description = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(50), default='system')  # system, telegram

    def __repr__(self):
        return f"<Setting(name={self.param_name}, value={self.param_value})>"


class SymbolList(Base):
    """Чёрный/белый список символов"""
    __tablename__ = 'symbol_lists'

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False, index=True)
    list_type = Column(String(10), nullable=False, index=True)  # blacklist, whitelist
    reason = Column(Text)
    added_at = Column(DateTime, default=datetime.utcnow)
    added_by = Column(String(50), default='telegram')  # telegram, system

    def __repr__(self):
        return f"<SymbolList(symbol={self.symbol}, type={self.list_type})>"


class SystemHealth(Base):
    """История здоровья системы"""
    __tablename__ = 'system_health'

    id = Column(Integer, primary_key=True, autoincrement=True)
    component = Column(String(50), nullable=False, index=True)  # ws, api, telegram, db
    status = Column(String(10), nullable=False)  # ok, error, warning
    message = Column(Text)
    response_time_ms = Column(Integer)
    checked_at = Column(DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<SystemHealth(component={self.component}, status={self.status})>"


class ErrorLog(Base):
    """Лог ошибок и инцидентов"""
    __tablename__ = 'errors_log'

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(10), nullable=False, index=True)  # ERROR, CRITICAL, WARNING
    component = Column(String(50), nullable=False, index=True)  # api, trading, ws, telegram, risk
    message = Column(Text, nullable=False)
    details = Column(Text)  # traceback или доп. данные
    symbol = Column(String(50), index=True)  # Символ если ошибка связана с контрактом
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<ErrorLog(level={self.level}, component={self.component})>"


class PnlHistory(Base):
    """История P&L по дням"""
    __tablename__ = 'pnl_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(DateTime, nullable=False, index=True)  # Дата (начало дня UTC)
    realized_pnl = Column(DECIMAL(20, 8), default=0)  # Реализованный PnL за день
    unrealized_pnl = Column(DECIMAL(20, 8), default=0)  # Нереализованный PnL на конец дня
    total_trades = Column(Integer, default=0)  # Кол-во сделок за день
    winning_trades = Column(Integer, default=0)  # Прибыльных сделок
    losing_trades = Column(Integer, default=0)  # Убыточных сделок
    total_volume_usdt = Column(DECIMAL(20, 8), default=0)  # Общий объём за день
    balance_start = Column(DECIMAL(20, 8))  # Баланс на начало дня
    balance_end = Column(DECIMAL(20, 8))  # Баланс на конец дня
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<PnlHistory(date={self.date}, pnl={self.realized_pnl})>"


# Индексы
Index('idx_trades_symbol_type', Trade.contract_symbol, Trade.trade_type)
Index('idx_positions_symbol_status', Position.contract_symbol, Position.status)
Index('idx_symbol_list_unique', SymbolList.symbol, SymbolList.list_type, unique=True)
Index('idx_health_checked', SystemHealth.component, SystemHealth.checked_at)
Index('idx_errors_component_time', ErrorLog.component, ErrorLog.created_at)
Index('idx_pnl_history_date', PnlHistory.date, unique=True)
