"""
Торговый модуль
Управление позициями SHORT, открытие/закрытие, сетка усреднений
Стратегия: SHORT на новых листингах с усреднением при росте цены
"""
import asyncio
import logging
import time as _time
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import make_transient

from src.api.gate_client import GateApiClient
from src.db.models import Position, AveragingHistory, Trade, Contract, SymbolList
from src.db.connection import db
from src.utils.config import config

logger = logging.getLogger(__name__)


class PositionManager:
    """Менеджер позиций SHORT"""

    def __init__(self):
        self.api_client = GateApiClient()
        self._active_positions: Dict[str, Position] = {}
        self._last_db_price_update: Dict[str, float] = {}  # symbol -> timestamp последнего обновления цены в БД

    def _is_blacklisted(self, symbol: str) -> bool:
        """Проверить, находится ли символ в чёрном списке"""
        try:
            with db.get_session() as session:
                entry = session.query(SymbolList).filter(
                    SymbolList.symbol == symbol,
                    SymbolList.list_type == 'blacklist'
                ).first()
                return entry is not None
        except Exception as e:
            logger.error(f"Ошибка проверки blacklist для {symbol}: {e}")
            return False

    def _get_trading_settings(self) -> Dict:
        """Получить торговые настройки из БД"""
        from src.db.settings import SettingsManager
        with db.get_session() as session:
            settings = SettingsManager(session)
            return {
                'max_concurrent_coins': settings.get('max_concurrent_coins', 10),
                'max_avg_count': settings.get('max_avg_count', 3),
                'avg_levels': settings.get('avg_levels', [300, 700, 1000]),
                'take_profit_pct': settings.get('take_profit_pct', 2.0),
                'ath_ratio_threshold': settings.get('ath_ratio_threshold', 0.3),
                'days_since_listing_limit': settings.get('days_since_listing_limit', 30),
            }

    async def open_position(
        self,
        symbol: str,
        entry_price: float,
        volume_usdt: float,
    ) -> Optional[Position]:
        """
        Открыть позицию SHORT

        Args:
            symbol: Символ контракта
            entry_price: Цена входа
            volume_usdt: Объем в USDT

        Returns:
            Объект позиции или None
        """
        try:
            # Проверяем blacklist
            if self._is_blacklisted(symbol):
                logger.info(f"Символ {symbol} в чёрном списке, пропускаем")
                return None

            # Получаем настройки из БД
            ts = self._get_trading_settings()

            # Если max_concurrent_coins = 0 - торговля остановлена
            if ts['max_concurrent_coins'] == 0:
                logger.info("Торговля остановлена (max_concurrent_coins = 0)")
                return None

            # Проверяем лимит позиций
            if len(self._active_positions) >= ts['max_concurrent_coins']:
                logger.warning(f"Достигнут лимит открытых позиций: {ts['max_concurrent_coins']}")
                return None

            # Проверяем что позиция еще не открыта
            if symbol in self._active_positions:
                logger.warning(f"Позиция для {symbol} уже открыта")
                return None

            # Проверяем дни с листинга
            days_since = self._get_days_since_listing(symbol)
            if days_since is None:
                logger.warning(f"Позиция не открыта: {symbol} - не удалось определить дату листинга")
                return None
            if days_since > ts['days_since_listing_limit']:
                logger.warning(f"Позиция не открыта: {symbol} - листинг старше {days_since} дней")
                return None

            # Проверяем ATH ratio
            ath_ratio = self._get_ath_ratio(symbol, entry_price)
            if ath_ratio is not None and ath_ratio < ts['ath_ratio_threshold']:
                logger.warning(f"Позиция не открыта: {symbol} - ATH ratio {ath_ratio:.3f} < {ts['ath_ratio_threshold']}")
                return None

            # Отправляем ордер на биржу (SHORT = отрицательный size)
            order_id = None
            actual_fill_price = entry_price
            actual_fill_volume = volume_usdt
            if not config.dry_run:
                # Получаем информацию о контракте (quanto_multiplier и max leverage)
                contract_info = await self.api_client.get_contract_info(symbol)
                if not contract_info:
                    logger.error(f"Не удалось получить информацию о контракте {symbol}")
                    return None

                quanto = float(contract_info.get('quanto_multiplier', 1) or 1)
                max_leverage = int(contract_info.get('leverage_max', 20) or 20)
                maintenance_rate = float(contract_info.get('maintenance_rate', 0.05) or 0.05)
                taker_fee_rate = float(contract_info.get('taker_fee_rate', 0.00075) or 0.00075)

                actual_leverage = await self.api_client.set_leverage(symbol, leverage=max_leverage)
                if actual_leverage == 0:
                    logger.error(f"Не удалось установить leverage для {symbol}")
                    return None

                # Рассчитываем size в контрактах с учётом quanto_multiplier
                if entry_price > 0 and quanto > 0:
                    size = -int(volume_usdt / (entry_price * quanto))
                else:
                    size = -1
                if size == 0:
                    size = -1  # Минимум 1 контракт

                # Оценка маржи Gate.io: номинал * (1/leverage + maintenance + 2*taker_fee)
                nominal = abs(size) * quanto * entry_price
                margin_rate = (1.0 / actual_leverage) + maintenance_rate + 2 * taker_fee_rate
                est_margin = nominal * margin_rate
                logger.info(
                    f"Ордер {symbol}: volume=${volume_usdt:.2f}, size={size}, "
                    f"quanto={quanto}, x{actual_leverage}, "
                    f"номинал=${nominal:.2f}, маржа~${est_margin:.2f}"
                )

                order_result = await self.api_client.place_futures_order(
                    contract=symbol,
                    size=size,
                    price="0",  # Рыночный ордер
                    tif="ioc",
                )

                # Обработка INSUFFICIENT_AVAILABLE — уменьшаем size и пробуем снова
                if isinstance(order_result, dict) and order_result.get('_error') == 'INSUFFICIENT_AVAILABLE':
                    raw = order_result.get('_raw', '')
                    logger.warning(f"Недостаточно маржи для {symbol}, пробуем уменьшить размер: {raw}")
                    # Пробуем size - 1 (минимальный шаг) до минимума
                    for reduced_size in range(abs(size) - 1, 0, -1):
                        reduced_nominal = reduced_size * quanto * entry_price
                        reduced_margin = reduced_nominal * margin_rate
                        logger.info(f"Повтор {symbol}: size=-{reduced_size}, маржа~${reduced_margin:.2f}")
                        order_result = await self.api_client.place_futures_order(
                            contract=symbol,
                            size=-reduced_size,
                            price="0",
                            tif="ioc",
                        )
                        if isinstance(order_result, dict) and order_result.get('_error') == 'INSUFFICIENT_AVAILABLE':
                            continue  # Всё ещё не хватает — уменьшаем дальше
                        break  # Успех или другая ошибка — выходим
                    else:
                        logger.error(f"Не удалось разместить ордер {symbol} даже с size=1")
                        return None

                if not order_result or (isinstance(order_result, dict) and '_error' in order_result):
                    logger.error(f"Не удалось разместить ордер SHORT для {symbol}")
                    return None

                # Проверяем исполнение IOC-ордера
                left = int(order_result.get('left', 0) or 0)
                order_size = int(order_result.get('size', 0) or 0)
                filled = abs(order_size) - abs(left)
                if filled <= 0:
                    logger.warning(f"IOC ордер для {symbol} не исполнен (left={left}, size={order_size}), пропускаем")
                    return None

                order_id = str(order_result.get('id', ''))
                # Используем реальную цену исполнения
                fill_price_str = order_result.get('fill_price', '0')
                if fill_price_str and float(fill_price_str) > 0:
                    actual_fill_price = float(fill_price_str)
                actual_fill_volume = abs(filled) * quanto * actual_fill_price
                logger.info(f"Ордер исполнен: {symbol} filled={filled}/{abs(order_size)} @ ${actual_fill_price:.6f}")
            else:
                contract_info = await self.api_client.get_contract_info(symbol)
                quanto = float(contract_info.get('quanto_multiplier', 1) or 1) if contract_info else 1
                max_leverage = int(contract_info.get('leverage_max', 20) or 20) if contract_info else 20
                logger.info(f"[DRY_RUN] Открытие SHORT {symbol} @ ${entry_price:.6f} (${volume_usdt:.0f} USDT, x{max_leverage})")

            # Создаем запись в БД (используем реальные данные исполнения)
            with db.get_session() as session:
                position = Position(
                    contract_symbol=symbol,
                    entry_price=actual_fill_price,
                    initial_entry_price=actual_fill_price,
                    current_price=actual_fill_price,
                    total_volume_usdt=actual_fill_volume,
                    avg_count=0,
                    status='open',
                    days_since_listing=days_since or 0,
                )

                # Записываем торговую операцию
                trade = Trade(
                    contract_symbol=symbol,
                    trade_type='open',
                    price=actual_fill_price,
                    volume_usdt=actual_fill_volume,
                    order_id=order_id,
                )
                session.add(position)
                session.add(trade)
                session.commit()
                session.refresh(position)

                # Отсоединяем от сессии для использования вне контекста
                session.expunge(position)
                make_transient(position)

                # Добавляем в активные
                self._active_positions[symbol] = position

                if ath_ratio:
                    logger.info(
                        f"🟢 SHORT открыт: {symbol} @ ${entry_price:.6f} "
                        f"(${volume_usdt:.0f} USDT), ATH ratio: {ath_ratio:.3f}"
                    )
                else:
                    logger.info(
                        f"🟢 SHORT открыт: {symbol} @ ${entry_price:.6f} (${volume_usdt:.0f} USDT)"
                    )

                return position

        except Exception as e:
            logger.error(f"Ошибка открытия позиции {symbol}: {e}")
            return None

    async def add_averaging(
        self,
        symbol: str,
        price: float,
        volume_usdt: float,
        avg_number: int,
        avg_level_pct: float,
    ) -> bool:
        """
        Добавить усреднение к позиции SHORT

        Усреднение происходит при РОСТЕ цены от цены входа.
        Уровни: 300%, 700%, 1000% от цены входа

        Args:
            symbol: Символ контракта
            price: Цена усреднения
            volume_usdt: Объем в USDT
            avg_number: Номер усреднения (1, 2, 3)
            avg_level_pct: Уровень в процентах (300, 700, 1000)

        Returns:
            True если успешно
        """
        try:
            if symbol not in self._active_positions:
                logger.warning(f"Позиция для {symbol} не найдена")
                return False

            position = self._active_positions[symbol]

            # Проверяем лимит усреднений (из БД)
            ts = self._get_trading_settings()
            if position.avg_count >= ts['max_avg_count']:
                logger.warning(f"Достигнут лимит усреднений для {symbol}")
                return False

            # Проверяем, не было ли уже усреднения на этом уровне
            if self._averaging_level_used(symbol, avg_level_pct):
                logger.info(f"Усреднение на уровне {avg_level_pct}% уже было для {symbol}")
                return False

            # Отправляем ордер на биржу (SHORT = отрицательный size)
            actual_avg_price = price
            actual_avg_volume = volume_usdt
            if not config.dry_run:
                # Получаем quanto_multiplier для расчёта size
                contract_info = await self.api_client.get_contract_info(symbol)
                quanto = float(contract_info.get('quanto_multiplier', 1) or 1) if contract_info else 1

                if price > 0 and quanto > 0:
                    size = -int(volume_usdt / (price * quanto))
                else:
                    size = -1
                if size == 0:
                    size = -1
                order_result = await self.api_client.place_futures_order(
                    contract=symbol,
                    size=size,
                    price="0",
                    tif="ioc",
                )
                if not order_result:
                    logger.error(f"Не удалось разместить ордер усреднения для {symbol}")
                    return False

                # Проверяем исполнение
                order_size = int(order_result.get('size', 0) or 0)
                left = int(order_result.get('left', 0) or 0)
                filled = abs(order_size) - abs(left)
                if filled <= 0:
                    logger.warning(f"IOC ордер усреднения для {symbol} не исполнен, пропускаем")
                    return False

                fill_price_str = order_result.get('fill_price', '0')
                if fill_price_str and float(fill_price_str) > 0:
                    actual_avg_price = float(fill_price_str)
                actual_avg_volume = abs(filled) * quanto * actual_avg_price
            else:
                logger.info(f"[DRY_RUN] Усреднение #{avg_number} для {symbol} @ ${price:.6f} (${volume_usdt:.0f} USDT)")

            # Вычисляем новую среднюю цену входа (используем реальные данные)
            # Для SHORT: средняя цена = (сумма объемов * цены) / общий объем
            old_volume = float(position.total_volume_usdt)
            old_avg_price = float(position.entry_price)

            total_volume = old_volume + actual_avg_volume
            new_avg_price = (
                (old_avg_price * old_volume + actual_avg_price * actual_avg_volume) / total_volume
            )

            # Обновляем в БД
            with db.get_session() as session:
                # Получаем свежие данные
                db_position = session.query(Position).filter(
                    Position.contract_symbol == symbol,
                    Position.status == 'open'
                ).first()

                if not db_position:
                    logger.error(f"Позиция {symbol} не найдена в БД")
                    return False

                # Обновляем параметры
                db_position.avg_count += 1
                db_position.entry_price = new_avg_price  # Новая средняя цена
                db_position.total_volume_usdt = total_volume
                db_position.current_price = actual_avg_price

                # Создаем запись об усреднении
                avg_history = AveragingHistory(
                    position_id=db_position.id,
                    contract_symbol=symbol,
                    avg_number=avg_number,
                    avg_level_pct=avg_level_pct,
                    avg_amount_usdt=actual_avg_volume,
                    avg_price=actual_avg_price,
                    avg_entry_price=new_avg_price,
                )
                session.add(avg_history)

                # Записываем торговую операцию
                trade = Trade(
                    contract_symbol=symbol,
                    trade_type='avg_open',
                    price=actual_avg_price,
                    volume_usdt=actual_avg_volume,
                )
                session.add(trade)

                session.commit()
                session.refresh(db_position)

                # Отсоединяем от сессии для использования вне контекста
                session.expunge(db_position)
                make_transient(db_position)

                # Обновляем в памяти
                self._active_positions[symbol] = db_position

                logger.info(
                    f"📊 Усреднение #{avg_number} для {symbol}: "
                    f"@ ${price:.6f} (${volume_usdt:.0f} USDT), "
                    f"уровень {avg_level_pct}%, "
                    f"новая средняя: ${new_avg_price:.6f}"
                )

                return True

        except Exception as e:
            logger.error(f"Ошибка усреднения {symbol}: {e}")
            return False

    async def close_position(
        self,
        symbol: str,
        exit_price: float,
        reason: str = 'manual',
    ) -> bool:
        """
        Закрыть позицию SHORT

        Для SHORT: PnL = (entry - exit) * volume / entry
        Прибыль когда exit < entry (цена упала)

        Args:
            symbol: Символ контракта
            exit_price: Цена выхода
            reason: Причина закрытия (manual, tp, timeout)

        Returns:
            True если успешно
        """
        try:
            if symbol not in self._active_positions:
                logger.warning(f"Позиция для {symbol} не найдена")
                return False

            position = self._active_positions[symbol]

            # Закрываем на бирже
            order_id = None
            if not config.dry_run:
                # Сначала пробуем close=True (single mode)
                order_result = await self.api_client.place_futures_order(
                    contract=symbol,
                    size=0,
                    price="0",
                    tif="ioc",
                    close=True,
                )
                if not order_result:
                    # Если не получилось (dual mode) — используем auto_size
                    # Требования API: size=0, reduce_only=true, close=false
                    logger.info(f"Повторная попытка закрытия {symbol} через auto_size (dual mode)")
                    order_result = await self.api_client.place_futures_order(
                        contract=symbol,
                        size=0,
                        price="0",
                        tif="ioc",
                        close=False,
                        reduce_only=True,
                        auto_size="close_short",
                    )
                if not order_result:
                    logger.error(f"Не удалось закрыть позицию на бирже для {symbol}")
                    return False
                order_id = str(order_result.get('id', ''))
                # Используем реальную цену исполнения с биржи
                fill_price_str = order_result.get('fill_price', '0')
                if fill_price_str and float(fill_price_str) > 0:
                    exit_price = float(fill_price_str)
                    logger.info(f"Реальная цена закрытия {symbol}: ${exit_price:.6f}")
            else:
                logger.info(f"[DRY_RUN] Закрытие {symbol} @ ${exit_price:.6f} (причина: {reason})")

            # Вычисляем PnL для SHORT (используем Decimal для точности)
            d_entry = Decimal(str(position.entry_price))
            d_exit = Decimal(str(exit_price))
            d_volume = Decimal(str(position.total_volume_usdt))

            # Для SHORT: PnL = (entry - exit) / entry * 100
            pnl_pct = float((d_entry - d_exit) / d_entry * 100)
            pnl_usdt = float(d_volume * (d_entry - d_exit) / d_entry)

            # Обновляем в БД
            with db.get_session() as session:
                db_position = session.query(Position).filter(
                    Position.contract_symbol == symbol,
                    Position.status == 'open'
                ).first()

                if not db_position:
                    logger.error(f"Позиция {symbol} не найдена в БД")
                    return False

                db_position.status = 'closed'
                db_position.current_price = exit_price
                db_position.unrealized_pnl = pnl_usdt
                db_position.closed_at = datetime.utcnow()

                # Записываем торговую операцию
                trade_type = 'tp_close' if reason == 'tp' else 'timeout_close' if reason == 'timeout' else 'close'

                trade = Trade(
                    contract_symbol=symbol,
                    trade_type=trade_type,
                    price=exit_price,
                    volume_usdt=float(d_volume),
                    pnl=pnl_usdt,
                    order_id=order_id,
                )
                session.add(trade)

                session.commit()

                # Удаляем из активных
                del self._active_positions[symbol]

                logger.info(
                    f"🔴 SHORT закрыт: {symbol} @ ${exit_price:.6f} "
                    f"PnL: ${pnl_usdt:+.2f} ({pnl_pct:+.2f}%) "
                    f"[{reason}]"
                )

                return True

        except Exception as e:
            logger.error(f"Ошибка закрытия позиции {symbol}: {e}")
            return False

    async def reopen_position(
        self,
        symbol: str,
        new_entry_price: float,
        volume_usdt: float,
    ) -> Optional[Position]:
        """
        Переоткрыть позицию SHORT после закрытия по TP

        Args:
            symbol: Символ контракта
            new_entry_price: Новая цена входа
            volume_usdt: Объем в USDT

        Returns:
            Объект позиции или None
        """
        try:
            # Получаем настройки из БД
            ts = self._get_trading_settings()

            # Проверяем условия для переоткрытия
            ath_ratio = self._get_ath_ratio(symbol, new_entry_price)
            if ath_ratio is not None and ath_ratio < ts['ath_ratio_threshold']:
                logger.info(f"Переоткрытие отменено: {symbol} - ATH ratio {ath_ratio:.3f} < {ts['ath_ratio_threshold']}")
                return None

            days_since = self._get_days_since_listing(symbol)
            if days_since is None:
                logger.info(f"Переоткрытие отменено: {symbol} - не удалось определить дату листинга")
                return None
            if days_since > ts['days_since_listing_limit']:
                logger.info(f"Переоткрытие отменено: {symbol} - листинг старше {days_since} дней")
                return None

            # Отправляем ордер на биржу (SHORT = отрицательный size)
            order_id = None
            actual_reopen_price = new_entry_price
            actual_reopen_volume = volume_usdt
            if not config.dry_run:
                contract_info = await self.api_client.get_contract_info(symbol)
                quanto = float(contract_info.get('quanto_multiplier', 1) or 1) if contract_info else 1

                if new_entry_price > 0 and quanto > 0:
                    size = -int(volume_usdt / (new_entry_price * quanto))
                else:
                    size = -1
                if size == 0:
                    size = -1
                order_result = await self.api_client.place_futures_order(
                    contract=symbol,
                    size=size,
                    price="0",
                    tif="ioc",
                )
                if not order_result:
                    logger.error(f"Не удалось разместить ордер переоткрытия для {symbol}")
                    return None

                # Проверяем исполнение
                order_size = int(order_result.get('size', 0) or 0)
                left = int(order_result.get('left', 0) or 0)
                filled = abs(order_size) - abs(left)
                if filled <= 0:
                    logger.warning(f"IOC ордер переоткрытия для {symbol} не исполнен, пропускаем")
                    return None

                order_id = str(order_result.get('id', ''))
                fill_price_str = order_result.get('fill_price', '0')
                if fill_price_str and float(fill_price_str) > 0:
                    actual_reopen_price = float(fill_price_str)
                actual_reopen_volume = abs(filled) * quanto * actual_reopen_price
            else:
                logger.info(f"[DRY_RUN] Переоткрытие SHORT {symbol} @ ${new_entry_price:.6f} (${volume_usdt:.0f} USDT)")

            # Обновляем существующую запись в БД (согласно ТЗ — не создаём новую)
            with db.get_session() as session:
                # Ищем последнюю закрытую позицию для этого символа
                position = session.query(Position).filter(
                    Position.contract_symbol == symbol,
                    Position.status == 'closed'
                ).order_by(Position.closed_at.desc()).first()

                if position:
                    # Обновляем существующую позицию
                    position.entry_price = actual_reopen_price
                    position.initial_entry_price = actual_reopen_price
                    position.current_price = actual_reopen_price
                    position.total_volume_usdt = actual_reopen_volume
                    position.avg_count = 0  # Сбрасываем счетчик усреднений
                    position.status = 'open'
                    position.unrealized_pnl = None
                    position.closed_at = None
                    position.opened_at = datetime.utcnow()
                    position.days_since_listing = days_since or 0
                else:
                    # Если закрытая позиция не найдена — создаём новую
                    position = Position(
                        contract_symbol=symbol,
                        entry_price=actual_reopen_price,
                        initial_entry_price=actual_reopen_price,
                        current_price=actual_reopen_price,
                        total_volume_usdt=actual_reopen_volume,
                        avg_count=0,
                        status='open',
                        days_since_listing=days_since or 0,
                    )
                    session.add(position)

                # Записываем торговую операцию
                trade = Trade(
                    contract_symbol=symbol,
                    trade_type='reopen',
                    price=actual_reopen_price,
                    volume_usdt=actual_reopen_volume,
                    order_id=order_id,
                )
                session.add(trade)
                session.commit()
                session.refresh(position)

                # Отсоединяем от сессии для использования вне контекста
                session.expunge(position)
                make_transient(position)

                # Добавляем в активные
                self._active_positions[symbol] = position

                logger.info(
                    f"🔄 SHORT переоткрыт: {symbol} @ ${new_entry_price:.6f} "
                    f"(${volume_usdt:.0f} USDT)"
                )

                return position

        except Exception as e:
            logger.error(f"Ошибка переоткрытия позиции {symbol}: {e}")
            return None

    async def update_position_price(self, symbol: str, current_price: float) -> bool:
        """
        Обновить текущую цену позиции.
        Обновление в БД не чаще 1 раза в 2 секунды (throttle),
        в памяти — всегда мгновенно.

        Args:
            symbol: Символ контракта
            current_price: Текущая цена

        Returns:
            True если успешно
        """
        try:
            if symbol not in self._active_positions:
                return False

            position = self._active_positions[symbol]

            # Вычисляем unrealized PnL для SHORT
            entry_price = float(position.entry_price)
            volume_usdt = float(position.total_volume_usdt)
            pnl_pct = (entry_price - current_price) / entry_price * 100
            pnl_usdt = volume_usdt * pnl_pct / 100

            # Обновляем в памяти всегда (мгновенно)
            position.current_price = current_price
            position.unrealized_pnl = pnl_usdt

            # Обновляем в БД только каждые 2 секунды (throttle)
            now = _time.time()
            last_update = self._last_db_price_update.get(symbol, 0)
            if now - last_update < 2.0:
                return True

            self._last_db_price_update[symbol] = now

            with db.get_session() as session:
                db_position = session.query(Position).filter(
                    Position.contract_symbol == symbol,
                    Position.status == 'open'
                ).first()

                if db_position:
                    db_position.current_price = current_price
                    db_position.unrealized_pnl = pnl_usdt
                    session.commit()

            return True

        except Exception as e:
            logger.error(f"Ошибка обновления цены {symbol}: {e}")
            return False

    def get_position(self, symbol: str) -> Optional[Position]:
        """Получить позицию по символу"""
        return self._active_positions.get(symbol)

    def get_all_positions(self) -> Dict[str, Position]:
        """Получить все активные позиции"""
        return self._active_positions.copy()

    def should_add_averaging(self, symbol: str, current_price: float) -> Optional[Tuple[int, float]]:
        """
        Проверить нужно ли добавлять усреднение для SHORT

        Усреднение происходит при РОСТЕ цены от цены входа.
        Уровни: 300%, 700%, 1000%

        growth_pct = ((current_price - entry_price) / entry_price) * 100

        Args:
            symbol: Символ контракта
            current_price: Текущая цена

        Returns:
            (avg_number, avg_level_pct) или None
        """
        if symbol not in self._active_positions:
            return None

        position = self._active_positions[symbol]
        # Используем initial_entry_price для расчёта уровней усреднения (не меняется после усреднений)
        entry_price = float(position.initial_entry_price or position.entry_price)

        # Получаем настройки из БД
        ts = self._get_trading_settings()

        # Проверяем лимит усреднений
        if position.avg_count >= ts['max_avg_count']:
            return None

        # Вычисляем рост цены в процентах (для SHORT - это рост)
        growth_pct = ((current_price - entry_price) / entry_price) * 100

        # Проверяем уровни усреднения
        for i, level in enumerate(ts['avg_levels']):
            avg_number = i + 1

            # Пропускаем уже использованные уровни
            if self._averaging_level_used(symbol, level):
                continue

            # Если цена выросла до уровня усреднения
            if growth_pct >= level:
                logger.info(
                    f"📊 Сигнал усреднения #{avg_number} для {symbol}: "
                    f"рост {growth_pct:.1f}% >= уровня {level}%"
                )
                return (avg_number, level)

        return None

    def should_close_position(
        self,
        symbol: str,
        current_price: float,
    ) -> Optional[str]:
        """
        Проверить нужно ли закрывать позицию SHORT

        Для SHORT:
        - TP: цена упала на 2% от входа (profit)
        - Timeout: позиция открыта больше N часов

        Args:
            symbol: Символ контракта
            current_price: Текущая цена

        Returns:
            Причина закрытия ('tp', 'timeout') или None
        """
        if symbol not in self._active_positions:
            return None

        position = self._active_positions[symbol]

        # Получаем настройки из БД
        ts = self._get_trading_settings()

        # Для SHORT: profit когда цена упала (используем Decimal для точности)
        d_entry = Decimal(str(position.entry_price))
        d_current = Decimal(str(current_price))
        change_pct = float((d_entry - d_current) / d_entry * 100)

        # Тейк-профит (цена упала на X%)
        if change_pct >= ts['take_profit_pct']:
            logger.info(
                f"🎯 Сигнал TP для {symbol}: "
                f"{change_pct:.2f}% >= {ts['take_profit_pct']:.2f}%"
            )
            return 'tp'

        # Стоп-лосс НЕ используется по стратегии

        # Таймаут позиции
        if position.opened_at:
            time_elapsed = datetime.utcnow() - position.opened_at
            if time_elapsed >= timedelta(hours=config.trading.position_timeout_hours):
                logger.info(
                    f"⏰ Сигнал TIMEOUT для {symbol}: "
                    f"{time_elapsed.total_seconds() / 3600:.1f} часов"
                )
                return 'timeout'

        return None

    def can_reopen(self, symbol: str, current_price: float) -> bool:
        """
        Проверить можно ли переоткрыть позицию после TP

        Условия:
        - ATH ratio > threshold
        - Дней с листинга < limit

        Args:
            symbol: Символ контракта
            current_price: Текущая цена

        Returns:
            True если можно переоткрыть
        """
        ts = self._get_trading_settings()

        ath_ratio = self._get_ath_ratio(symbol, current_price)
        if ath_ratio is not None and ath_ratio < ts['ath_ratio_threshold']:
            return False

        days_since = self._get_days_since_listing(symbol)
        if days_since is None:
            return False
        if days_since > ts['days_since_listing_limit']:
            return False

        return True

    def _get_days_since_listing(self, symbol: str) -> Optional[int]:
        """Получить количество дней с листинга. Возвращает None если не удалось определить."""
        try:
            with db.get_session() as session:
                contract = session.query(Contract).filter(
                    Contract.symbol == symbol
                ).first()

                if contract and contract.launch_time:
                    delta = datetime.utcnow() - contract.launch_time
                    return delta.days

        except Exception as e:
            logger.error(f"Ошибка получения дней с листинга: {e}")

        return None

    def _get_ath_ratio(self, symbol: str, current_price: float) -> Optional[float]:
        """
        Получить ATH ratio = current_price / ath_price

        Args:
            symbol: Символ контракта
            current_price: Текущая цена

        Returns:
            ATH ratio или None
        """
        try:
            with db.get_session() as session:
                contract = session.query(Contract).filter(
                    Contract.symbol == symbol
                ).first()

                if contract and contract.ath_price:
                    ath = float(contract.ath_price)
                    if ath > 0:
                        return current_price / ath

        except Exception as e:
            logger.error(f"Ошибка получения ATH ratio: {e}")

        return None

    def _averaging_level_used(self, symbol: str, level_pct: float) -> bool:
        """
        Проверить, было ли уже усреднение на этом уровне

        Args:
            symbol: Символ контракта
            level_pct: Уровень в процентах

        Returns:
            True если уровень уже использован
        """
        try:
            with db.get_session() as session:
                position = session.query(Position).filter(
                    Position.contract_symbol == symbol,
                    Position.status == 'open'
                ).first()

                if not position:
                    return False

                # Проверяем историю усреднений
                avg_history = session.query(AveragingHistory).filter(
                    AveragingHistory.position_id == position.id,
                    AveragingHistory.avg_level_pct == level_pct
                ).first()

                return avg_history is not None

        except Exception as e:
            logger.error(f"Ошибка проверки уровня усреднения: {e}")
            return False

    async def load_active_positions(self):
        """Загрузить активные позиции из БД"""
        try:
            with db.get_session() as session:
                positions = session.query(Position).filter(
                    Position.status == 'open'
                ).all()

                for position in positions:
                    session.expunge(position)
                    make_transient(position)
                    self._active_positions[position.contract_symbol] = position

                logger.info(f"Загружено {len(positions)} активных позиций")

        except Exception as e:
            logger.error(f"Ошибка загрузки активных позиций: {e}")

        # Синхронизируем с биржей (восстанавливаем потерянные позиции)
        await self._sync_positions_from_exchange()

    async def _sync_positions_from_exchange(self):
        """Синхронизировать позиции с биржей (восстановить потерянные при перезапуске)"""
        try:
            exchange_positions = await self.api_client.get_all_positions()

            if not exchange_positions:
                logger.info("На бирже нет открытых позиций")
                return

            synced = 0
            for pos in exchange_positions:
                symbol = pos.get('contract', '')
                size = int(pos.get('size', 0) or 0)

                if not symbol or size == 0:
                    continue

                # Если позиция уже загружена из БД — пропускаем
                if symbol in self._active_positions:
                    continue

                # Позиция есть на бирже, но нет в БД — восстанавливаем
                entry_price = float(pos.get('entry_price', 0) or 0)
                mark_price = float(pos.get('mark_price', 0) or 0)
                unrealised_pnl = float(pos.get('unrealised_pnl', 0) or 0)

                # quanto_multiplier не приходит в API позиций — берём из контракта
                quanto = 1.0
                try:
                    contract_info = await self.api_client.get_contract_info(symbol)
                    if contract_info:
                        quanto = float(contract_info.get('quanto_multiplier', 1) or 1)
                except Exception as e:
                    logger.warning(f"Не удалось получить quanto для {symbol}: {e}")

                volume_usdt = abs(size) * quanto * entry_price

                logger.warning(
                    f"🔄 Восстановление позиции с биржи: {symbol} "
                    f"size={size}, entry=${entry_price:.6f}, volume=${volume_usdt:.2f}"
                )

                with db.get_session() as session:
                    # Убеждаемся что контракт есть в БД
                    contract = session.query(Contract).filter(
                        Contract.symbol == symbol
                    ).first()
                    if not contract:
                        contract = Contract(
                            symbol=symbol,
                            launch_time=datetime.utcnow(),
                            status='in_work',
                            listing_taken_in_work=True,
                        )
                        session.add(contract)
                        session.flush()
                    elif not contract.listing_taken_in_work:
                        contract.listing_taken_in_work = True

                    position = Position(
                        contract_symbol=symbol,
                        entry_price=entry_price,
                        initial_entry_price=entry_price,
                        current_price=mark_price or entry_price,
                        total_volume_usdt=volume_usdt,
                        avg_count=0,
                        status='open',
                        days_since_listing=0,
                        unrealized_pnl=unrealised_pnl,
                    )
                    session.add(position)
                    session.commit()
                    session.refresh(position)
                    session.expunge(position)
                    make_transient(position)

                    self._active_positions[symbol] = position
                    synced += 1

            if synced > 0:
                logger.info(f"🔄 Восстановлено {synced} позиций с биржи")

        except Exception as e:
            logger.error(f"Ошибка синхронизации позиций с биржей: {e}")

    async def detect_externally_closed(self) -> List[str]:
        """
        Обнаружить позиции, закрытые вручную на бирже.
        Сравнивает _active_positions с реальными позициями на бирже.

        Returns:
            Список символов, которые были закрыты снаружи
        """
        if not self._active_positions:
            return []

        try:
            exchange_positions = await self.api_client.get_all_positions()
            exchange_symbols = set()
            for pos in (exchange_positions or []):
                symbol = pos.get('contract', '')
                size = int(pos.get('size', 0) or 0)
                if symbol and size != 0:
                    exchange_symbols.add(symbol)

            closed_externally = []
            for symbol in list(self._active_positions.keys()):
                if symbol not in exchange_symbols:
                    position = self._active_positions[symbol]
                    exit_price = float(position.current_price or position.entry_price)

                    logger.warning(
                        f"🔄 Позиция {symbol} закрыта вне бота "
                        f"(вручную на бирже). Удаляем из отслеживания."
                    )

                    # Обновляем БД
                    with db.get_session() as session:
                        db_position = session.query(Position).filter(
                            Position.contract_symbol == symbol,
                            Position.status == 'open'
                        ).first()

                        if db_position:
                            db_position.status = 'closed'
                            db_position.current_price = exit_price
                            db_position.closed_at = datetime.utcnow()

                            trade = Trade(
                                contract_symbol=symbol,
                                trade_type='external_close',
                                price=exit_price,
                                volume_usdt=float(position.total_volume_usdt),
                                pnl=0,  # Не знаем точный PnL
                            )
                            session.add(trade)

                        # Сбрасываем флаг, чтобы мониторинг мог переоткрыть
                        contract = session.query(Contract).filter(
                            Contract.symbol == symbol
                        ).first()
                        if contract:
                            contract.listing_taken_in_work = False

                        session.commit()

                    del self._active_positions[symbol]
                    closed_externally.append(symbol)

            return closed_externally

        except Exception as e:
            logger.error(f"Ошибка проверки внешнего закрытия позиций: {e}")
            return []

    async def cleanup_old_positions(self):
        """Закрыть просроченные позиции"""
        try:
            # Собираем список позиций для закрытия (без открытой сессии)
            positions_to_close = []

            with db.get_session() as session:
                positions = session.query(Position).filter(
                    Position.status == 'open'
                ).all()

                for position in positions:
                    if position.opened_at:
                        time_elapsed = datetime.utcnow() - position.opened_at
                        if time_elapsed >= timedelta(hours=config.trading.position_timeout_hours):
                            current_price = float(position.current_price or position.entry_price)
                            positions_to_close.append((position.contract_symbol, current_price))

            # Закрываем позиции вне сессии (close_position откроет свою)
            closed_count = 0
            for symbol, price in positions_to_close:
                await self.close_position(symbol, price, reason='timeout')
                closed_count += 1

            if closed_count > 0:
                logger.info(f"Закрыто {closed_count} просроченных позиций")

        except Exception as e:
            logger.error(f"Ошибка очистки старых позиций: {e}")


# Глобальный инстанс
position_manager = PositionManager()
