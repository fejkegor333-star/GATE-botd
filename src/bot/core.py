"""
Главный модуль оркестрации
Объединяет все модули в единую систему
Стратегия: SHORT на новых листингах с усреднением при росте цены
"""
import asyncio
import logging
import time
from typing import Optional
from datetime import datetime
from decimal import Decimal

from src.api.monitoring import monitor as listing_monitor
from src.api.websocket_client import ws_client
from src.db.models import Contract, SymbolList
from src.trading.trader import position_manager
from src.risk.risk_manager import risk_manager, balance_protection_checker
from src.utils.config import config
from src.api.websocket_client import OrderBook
from src.api.gate_client import GateApiClient
from src.db.connection import db
from src.db.error_logger import log_exception
from src.db.pnl_tracker import update_daily_pnl, save_daily_balance
from src.risk.acceleration import acceleration_manager

logger = logging.getLogger(__name__)


class TradingBot:
    """Главный класс бота"""

    def __init__(self, enable_telegram: bool = True):
        self._running = False
        self._tasks = []
        self._ws_task: Optional[asyncio.Task] = None
        self._notifier = None
        self.api_client = GateApiClient()
        self.enable_telegram = enable_telegram
        # Мьютексы для предотвращения race condition
        self._position_locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()  # Для безопасного создания per-symbol lock
        self._close_cooldowns: dict[str, float] = {}  # Кулдаун после неудачного закрытия
        self._last_exchange_sync: float = 0  # Время последней синхронизации с биржей
        self._notified_listings: set[str] = set()  # Символы, о которых уже отправлено уведомление
        self._reopen_counts: dict[str, int] = {}  # Счётчик переоткрытий за короткий период
        self._reopen_window_start: dict[str, float] = {}  # Начало окна подсчёта переоткрытий
        # Настройки мониторинга стакана
        self._orderbook_enabled: bool = True
        self._orderbook_throttle_ms: int = 100
        self._last_ob_update: dict[str, float] = {}  # symbol -> timestamp ms

    def _load_orderbook_settings(self):
        """Загрузить настройки мониторинга стакана из БД"""
        try:
            from src.db.settings import SettingsManager
            with db.get_session() as session:
                settings = SettingsManager(session)
                self._orderbook_enabled = settings.get('orderbook_monitoring_enabled', True)
                self._orderbook_throttle_ms = settings.get('orderbook_update_throttle_ms', 100)
            logger.debug(f"Настройки стакана: enabled={self._orderbook_enabled}, throttle={self._orderbook_throttle_ms}ms")
        except Exception as e:
            logger.warning(f"Ошибка загрузки настроек стакана: {e}")

    async def _calculate_position_size(self) -> float:
        """
        Рассчитать размер позиции.
        Если auto_position_size=True: свободный баланс * коэффициент.
        Иначе: ручной initial_position_usdt.
        """
        from src.db.settings import SettingsManager
        with db.get_session() as session:
            settings = SettingsManager(session)
            auto_mode = settings.get('auto_position_size', False)
            if not auto_mode:
                return float(settings.get('initial_position_usdt', 10.0))
            coefficient = float(settings.get('position_size_coefficient', 0.3))

        # Авто-режим: баланс * коэффициент
        balance_data = await self.api_client.get_futures_balance()
        available = float(balance_data.get('available', 0) or 0)
        if available <= 0:
            logger.warning("Авто-размер: свободный баланс = 0, используем ручной")
            from src.db.settings import SettingsManager as _SM2
            with db.get_session() as session:
                return float(_SM2(session).get('initial_position_usdt', 10.0))

        volume = round(available * coefficient, 2)
        logger.info(f"Авто-размер позиции: ${available:.2f} * {coefficient} = ${volume:.2f}")
        return volume

    async def _get_symbol_lock(self, symbol: str) -> asyncio.Lock:
        """Получить или создать lock для конкретного символа"""
        async with self._locks_lock:
            if symbol not in self._position_locks:
                self._position_locks[symbol] = asyncio.Lock()
            return self._position_locks[symbol]

    @property
    def notifier(self):
        """Ленивое получение notifier"""
        if self._notifier is None:
            from src.telegram.bot import get_notifier
            self._notifier = get_notifier()
        return self._notifier

    async def start(self):
        """Запустить все системы бота"""
        if self._running:
            logger.warning("Бот уже запущен")
            return

        self._running = True
        logger.info("Запуск торгового бота...")

        try:
            # 0. Загружаем настройки мониторинга стакана
            self._load_orderbook_settings()

            # 1. Загружаем активные позиции
            logger.info("Загрузка активных позиций...")
            await position_manager.load_active_positions()

            # 2. Запускаем мониторинг новых листингов
            logger.info("Запуск мониторинга новых листингов...")
            listing_monitor.on_new_listing(self._on_new_listing)
            await listing_monitor.start()

            # 3. Регистрируем callback на обновления стакана
            ws_client.on_order_book_update(self._on_order_book_update)

            # 3.1. Если есть активные позиции — подключаем WebSocket для мониторинга цен
            active_positions = position_manager.get_all_positions()
            if active_positions:
                logger.info(f"Подключение WebSocket для {len(active_positions)} активных позиций...")
                if not ws_client.is_connected():
                    await ws_client.connect()
                for symbol in active_positions:
                    await ws_client.subscribe_order_book(symbol)
                    logger.info(f"WebSocket подписка на {symbol} для мониторинга цены")
                self._ws_task = asyncio.create_task(self._websocket_listen_loop())
                self._tasks.append(self._ws_task)
            else:
                logger.info("WebSocket будет подключен при обнаружении нового листинга")

            # 4. Запускаем защиту баланса
            logger.info("Запуск защиты баланса...")
            await balance_protection_checker.start()

            # 5. Запускаем задачи (WebSocket loop запустится при новом листинге)
            self._tasks = [
                asyncio.create_task(self._positions_monitor_loop()),
                asyncio.create_task(self._cleanup_loop()),
                asyncio.create_task(self._ath_update_loop()),
            ]

            # Telegram бот (опционально)
            if self.enable_telegram:
                self._tasks.append(asyncio.create_task(self._telegram_polling()))
            else:
                logger.info("Telegram polling отключен")

            logger.info("✅ Бот успешно запущен!")

        except Exception as e:
            logger.error(f"Ошибка при запуске бота: {e}")
            await self.stop()
            raise

    async def stop(self):
        """Остановить все системы бота"""
        if not self._running:
            return

        logger.info("Остановка торгового бота...")
        self._running = False

        # Останавливаем задачи
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._tasks.clear()

        # Останавливаем мониторинг
        await listing_monitor.stop()

        # Останавливаем защиту баланса
        await balance_protection_checker.stop()

        # Отключаем WebSocket если подключен
        if ws_client.is_connected():
            await ws_client.disconnect()

        logger.info("Бот остановлен")

    async def _websocket_listen_loop(self):
        """Цикл прослушивания WebSocket с автоматическим переподключением"""
        while self._running:
            try:
                # Если нет подписок — выходим из цикла, нет смысла держать соединение
                if not ws_client.order_books:
                    logger.info("Нет активных подписок, WebSocket loop завершён")
                    break

                await ws_client.listen()
                # listen() завершился нормально (ConnectionClosed) — переподключаемся если есть подписки
                if not self._running:
                    break
                if not ws_client.order_books:
                    logger.info("Нет подписок после отключения, WebSocket loop завершён")
                    break
                logger.info("WebSocket отключился, переподключение через 5 сек...")
                await asyncio.sleep(5)
                await ws_client.reconnect()
            except asyncio.CancelledError:
                logger.info("WebSocket цикл отменен")
                break
            except Exception as e:
                logger.error(f"Ошибка в WebSocket цикле: {e}")
                if not self._running:
                    break
                if not ws_client.order_books:
                    logger.info("Нет подписок, WebSocket loop завершён")
                    break
                logger.info("Переподключение к WebSocket через 10 сек...")
                await asyncio.sleep(10)
                try:
                    await ws_client.reconnect()
                except Exception as reconnect_error:
                    logger.error(f"Ошибка переподключения: {reconnect_error}")

    def _cleanup_finished_tasks(self):
        """Очистить завершённые задачи из списка"""
        self._tasks = [t for t in self._tasks if not t.done()]

    async def _positions_monitor_loop(self):
        """Цикл мониторинга позиций"""
        while self._running:
            try:
                # Периодически перечитываем настройки стакана
                self._load_orderbook_settings()

                # REST fallback: если WS мониторинг отключён, обновляем цены через REST тикер
                if not self._orderbook_enabled:
                    await self._rest_price_update()

                await self._check_positions()
                # Периодически очищаем завершённые задачи
                self._cleanup_finished_tasks()
                await asyncio.sleep(5)  # Проверяем каждые 5 секунд
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга позиций: {e}")
                await asyncio.sleep(10)

    async def _rest_price_update(self):
        """REST fallback: обновить цены позиций через тикер когда WS отключён"""
        positions = position_manager.get_all_positions()
        for symbol in positions:
            try:
                ticker = await self.api_client.get_ticker(symbol)
                if ticker:
                    last_price = float(ticker.get('last', 0) or 0)
                    if last_price > 0:
                        await position_manager.update_position_price(symbol, last_price)
                        await self._check_position_signals(symbol, last_price)
            except Exception as e:
                logger.error(f"REST fallback ошибка для {symbol}: {e}")

    async def _cleanup_loop(self):
        """Цикл очистки старых позиций"""
        while self._running:
            try:
                await position_manager.cleanup_old_positions()
                await asyncio.sleep(60)  # Проверяем каждую минуту
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле очистки: {e}")
                await asyncio.sleep(30)

    async def _ath_update_loop(self):
        """Цикл обновления ATH для активных контрактов"""
        while self._running:
            try:
                positions = position_manager.get_all_positions()
                for symbol in positions.keys():
                    try:
                        await self.api_client.update_contract_ath(symbol)
                    except Exception as e:
                        logger.error(f"Ошибка обновления ATH для {symbol}: {e}")
                
                # Обновляем раз в час
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле обновления ATH: {e}")
                await asyncio.sleep(300)

    async def _telegram_polling(self):
        """Цикл обработки Telegram сообщений"""
        try:
            from src.telegram.bot import get_telegram_bot

            bot = get_telegram_bot()
            logger.info("Запуск Telegram polling...")

            # Удаляем webhook и ждём освобождения сессии
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    await bot.bot.delete_webhook(drop_pending_updates=True)
                    logger.info("Webhook удален перед polling")
                    # Ждём достаточно, чтобы предыдущий getUpdates timeout истёк
                    await asyncio.sleep(5)
                    break
                except Exception as e:
                    logger.warning(f"Не удалось удалить webhook (попытка {attempt + 1}): {e}")
                    await asyncio.sleep(3)

            # Запускаем polling
            await bot.dp.start_polling(
                bot.bot,
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"],
            )

        except asyncio.CancelledError:
            logger.info("Telegram polling отменен")
        except Exception as e:
            logger.error(f"Ошибка Telegram polling: {e}", exc_info=True)

    async def _on_new_listing(self, symbol: str, contract_data: dict):
        """
        Обработчик нового листинга от мониторинга.
        Проверяет готовность контракта к торговле, при необходимости ждёт начала торгов.
        Сразу открывает SHORT позицию.
        """
        try:
            # Проверяем что позиция ещё не открыта
            if position_manager.get_position(symbol):
                logger.info(f"Позиция для {symbol} уже открыта, пропускаем")
                listing_monitor.mark_listing_processed(symbol)
                return

            # Проверяем лимит одновременных позиций ДО отправки уведомления
            active_count = len(position_manager.get_all_positions())
            from src.db.settings import SettingsManager
            with db.get_session() as session:
                settings = SettingsManager(session)
                max_coins = settings.get('max_concurrent_coins', 10)
                whitelist_only = settings.get('whitelist_only', False)
            if max_coins > 0 and active_count >= max_coins:
                logger.info(f"Достигнут лимит позиций ({active_count}/{max_coins}), {symbol} в очередь")
                listing_monitor.mark_listing_failed(symbol, permanent=False, retry_minutes=5)
                return

            # Проверяем whitelist-only режим
            if whitelist_only:
                with db.get_session() as session:
                    in_whitelist = session.query(SymbolList).filter(
                        SymbolList.symbol == symbol,
                        SymbolList.list_type == 'whitelist'
                    ).first() is not None
                if not in_whitelist:
                    logger.info(f"Whitelist-only: {symbol} не в белом списке, пропускаем")
                    listing_monitor.mark_listing_processed(symbol)
                    return

            logger.info(f"🚀 Новый листинг обнаружен: {symbol}")

            # Уведомляем в Telegram о новом листинге (только первый раз)
            if symbol not in self._notified_listings:
                self._notified_listings.add(symbol)
                try:
                    create_time = contract_data.get('create_time')
                    if isinstance(create_time, (int, float)):
                        launch_time = datetime.utcfromtimestamp(create_time)
                    else:
                        launch_time = datetime.utcnow()
                    await self.notifier.send_new_listing(symbol, launch_time)
                except Exception:
                    pass

            # Проверяем готовность контракта к торговле
            trade_size = int(float(contract_data.get('trade_size', 0) or 0))
            last_price = float(contract_data.get('last_price', 0) or 0)

            if trade_size == 0 and last_price <= 0:
                logger.info(f"⏳ {symbol}: торги ещё не начались (trade_size=0, last_price=0). Ожидаем начала торгов...")
                try:
                    await self.notifier.send_listing_waiting(symbol)
                except Exception:
                    pass
                # Запускаем задачу ожидания начала торгов
                task = asyncio.create_task(self._wait_for_trading_start(symbol))
                self._tasks.append(task)
                return

            # Торги уже идут — открываем SHORT немедленно
            await self._open_short_immediately(symbol)

        except Exception as e:
            logger.error(f"Ошибка обработки нового листинга {symbol}: {e}", exc_info=True)
            log_exception('trading', f"Ошибка обработки нового листинга {symbol}", e, symbol=symbol)
            try:
                await self.notifier.send_error(f"Ошибка открытия {symbol}: {e}")
            except Exception:
                pass

    async def _wait_for_trading_start(self, symbol: str, max_wait_minutes: int = 30, poll_seconds: int = 3):
        """
        Ожидать начала торгов по контракту, опрашивая API каждые poll_seconds секунд.
        Как только торги начнутся — открываем SHORT.
        """
        logger.info(f"⏳ Запущено ожидание начала торгов для {symbol} (макс {max_wait_minutes} мин, опрос каждые {poll_seconds} сек)")
        start_time = datetime.utcnow()
        attempt = 0

        while self._running:
            try:
                elapsed = (datetime.utcnow() - start_time).total_seconds()
                if elapsed > max_wait_minutes * 60:
                    logger.warning(f"⏰ Таймаут ожидания торгов для {symbol} ({max_wait_minutes} мин)")
                    try:
                        await self.notifier.send_error(f"⏰ {symbol}: таймаут ожидания начала торгов ({max_wait_minutes} мин)")
                    except Exception:
                        pass
                    # Сбрасываем listing_taken_in_work чтобы повторить при перезапуске
                    try:
                        with db.get_session() as session:
                            contract = session.query(Contract).filter(Contract.symbol == symbol).first()
                            if contract:
                                contract.listing_taken_in_work = False
                                session.commit()
                    except Exception:
                        pass
                    listing_monitor.mark_listing_failed(symbol)
                    return

                attempt += 1

                # Проверяем через тикер — если last > 0, торги начались
                ticker = await self.api_client.get_ticker(symbol)
                if ticker:
                    last_price = float(ticker.get('last', 0) or 0)
                    if last_price > 0:
                        logger.info(f"🟢 {symbol}: торги начались! Цена: ${last_price:.6f} (ожидали {int(elapsed)} сек)")
                        try:
                            await self.notifier.send_listing_waiting(
                                symbol,
                                reason=f"Торги начались! Цена: ${last_price:.6f} (ожидали {int(elapsed)} сек)"
                            )
                        except Exception:
                            pass
                        await self._open_short_immediately(symbol)
                        return

                if attempt % 10 == 0:
                    logger.info(f"⏳ {symbol}: всё ещё ожидаем начала торгов... ({int(elapsed)} сек)")

                # Ждём ПОСЛЕ проверки, чтобы первая проверка была мгновенной
                await asyncio.sleep(poll_seconds)

            except asyncio.CancelledError:
                logger.info(f"Ожидание торгов для {symbol} отменено")
                listing_monitor.mark_listing_failed(symbol)
                return
            except Exception as e:
                logger.error(f"Ошибка при ожидании торгов {symbol}: {e}")
                await asyncio.sleep(poll_seconds)

    async def _open_short_immediately(self, symbol: str):
        """
        Открыть SHORT позицию немедленно через REST API.
        Опционально ждёт сигнала стакана (should_sell_signal) перед входом.
        """
        lock = await self._get_symbol_lock(symbol)
        async with lock:
            try:
                # Проверяем что позиция ещё не открыта (могла открыться за время ожидания)
                if position_manager.get_position(symbol):
                    logger.info(f"Позиция для {symbol} уже открыта, пропускаем")
                    listing_monitor.mark_listing_processed(symbol)
                    return

                # Опциональная проверка стакана перед входом
                from src.db.settings import SettingsManager as _SM
                with db.get_session() as session:
                    _settings = _SM(session)
                    check_ob = _settings.get('check_orderbook_before_entry', False)
                    min_liq = _settings.get('min_liquidity_usdt', 1000.0)

                if check_ob:
                    logger.info(f"⏳ {symbol}: ожидание сигнала стакана (should_sell_signal)...")
                    ob_ready = await self._wait_for_orderbook_signal(symbol, min_liq)
                    if not ob_ready:
                        logger.warning(f"{symbol}: стакан не готов, открываем без проверки")

                # Получаем текущую цену через REST API тикер
                ticker = await self.api_client.get_ticker(symbol)
                if not ticker:
                    logger.warning(f"Не удалось получить тикер для {symbol}")
                    listing_monitor.mark_listing_failed(symbol)
                    return

                entry_price = float(ticker.get('last', 0) or 0)
                if entry_price <= 0:
                    logger.warning(f"Невалидная цена для {symbol}: {entry_price}")
                    listing_monitor.mark_listing_failed(symbol)
                    return

                # Получаем объём позиции (авто или ручной) с учётом режима разгона
                base_volume = await self._calculate_position_size()
                volume_usdt = acceleration_manager.calculate_volume(symbol, base_volume)

                # Получаем параметры контракта для корректной проверки маржи
                contract_info = await self.api_client.get_contract_info(symbol)
                ci_leverage = int(contract_info.get('leverage_max', 10) or 10) if contract_info else 10
                ci_maintenance = float(contract_info.get('maintenance_rate', 0.05) or 0.05) if contract_info else 0.05
                ci_taker_fee = float(contract_info.get('taker_fee_rate', 0.00075) or 0.00075) if contract_info else 0.00075

                # Перепроверка contract_type из свежих данных контракта
                # (Gate.io может не заполнить contract_type сразу при создании)
                if contract_info:
                    from src.api.monitoring import _is_filtered_symbol
                    filter_reason = _is_filtered_symbol(symbol, contract_info)
                    if filter_reason:
                        logger.warning(f"Контракт {symbol} отфильтрован при открытии: {filter_reason}")
                        listing_monitor.mark_listing_processed(symbol)
                        return

                # Проверяем риск перед торговлей (с реальным плечом и ставками контракта)
                can_trade, reason = await risk_manager.check_before_trade(
                    volume_usdt, leverage=ci_leverage,
                    maintenance_rate=ci_maintenance, taker_fee_rate=ci_taker_fee
                )
                if not can_trade:
                    logger.warning(f"Нельзя торговать {symbol}: {reason}")
                    # Не помечаем как permanent — повторим на следующем цикле
                    # (может быть временная причина: макс. позиций, недостаточно баланса)
                    listing_monitor.mark_listing_failed(symbol, permanent=False)
                    return

                # Обновляем ATH перед первым трейдом (чтобы проверка ATH ratio сработала)
                try:
                    await self.api_client.update_contract_ath(symbol)
                except Exception as e:
                    logger.warning(f"Не удалось обновить ATH для {symbol} перед открытием: {e}")

                # Открываем SHORT позицию
                logger.info(f"🚀 Открываем SHORT: {symbol} @ ${entry_price:.6f}")
                position = await position_manager.open_position(
                    symbol,
                    entry_price,
                    volume_usdt,
                )

                if position:
                    logger.info(f"🟢 SHORT открыт: {symbol} @ ${entry_price:.6f}")

                    # Помечаем контракт как обработанный в БД
                    try:
                        with db.get_session() as session:
                            contract = session.query(Contract).filter(Contract.symbol == symbol).first()
                            if contract:
                                contract.listing_taken_in_work = True
                                session.commit()
                    except Exception:
                        pass
                    listing_monitor.mark_listing_processed(symbol)

                    await self.notifier.send_position_opened(
                        symbol,
                        entry_price,
                        volume_usdt,
                        leverage=ci_leverage,
                    )

                    # Подключаем WebSocket для мониторинга цены позиции
                    if not ws_client.is_connected():
                        await ws_client.connect()
                    if self._ws_task is None or self._ws_task.done():
                        self._ws_task = asyncio.create_task(self._websocket_listen_loop())
                        self._tasks.append(self._ws_task)
                    await ws_client.subscribe_order_book(symbol)
                    logger.info(f"WebSocket подписка на {symbol} для мониторинга цены")
                else:
                    logger.warning(f"Не удалось открыть позицию для {symbol}")
                    listing_monitor.mark_listing_failed(symbol)

            except Exception as e:
                logger.error(f"Ошибка открытия SHORT {symbol}: {e}", exc_info=True)
                log_exception('trading', f"Ошибка открытия SHORT {symbol}", e, symbol=symbol)
                listing_monitor.mark_listing_failed(symbol)
                try:
                    await self.notifier.send_error(f"Ошибка открытия {symbol}: {e}")
                except Exception:
                    pass

    async def _wait_for_orderbook_signal(self, symbol: str, min_liquidity_usdt: float, timeout_sec: int = 600) -> bool:
        """
        Ожидать сигнала стакана (should_sell_signal) до timeout_sec секунд.
        Подключает WS, подписывается на стакан, ждёт сигнала.

        Returns:
            True если сигнал получен, False если таймаут
        """
        try:
            # Подключаем WS если не подключён
            if not ws_client.is_connected():
                await ws_client.connect()
            await ws_client.subscribe_order_book(symbol)

            # Запускаем WS loop если не запущен
            if self._ws_task is None or self._ws_task.done():
                self._ws_task = asyncio.create_task(self._websocket_listen_loop())
                self._tasks.append(self._ws_task)

            start = time.time()
            while time.time() - start < timeout_sec:
                order_book = ws_client.get_order_book(symbol)
                if order_book and order_book.should_sell_signal(min_liquidity_usdt=min_liquidity_usdt):
                    logger.info(f"🟢 {symbol}: сигнал стакана получен!")
                    return True
                await asyncio.sleep(1)

            logger.warning(f"⏰ {symbol}: таймаут ожидания сигнала стакана ({timeout_sec}с)")
            return False

        except Exception as e:
            logger.error(f"Ошибка ожидания сигнала стакана {symbol}: {e}")
            return False

    async def _on_order_book_update(self, symbol: str, order_book: OrderBook):
        """
        Обработчик обновления стакана.
        Используется ТОЛЬКО для мониторинга цены уже открытых позиций.
        Вход в позицию происходит мгновенно в _open_short_immediately.
        """
        try:
            # Проверяем включён ли мониторинг стакана
            if not self._orderbook_enabled:
                return

            # Обновляем цену позиций если они есть
            if symbol not in position_manager.get_all_positions():
                return

            # Throttle: не обрабатывать чаще чем каждые N мс
            now = time.time() * 1000
            last = self._last_ob_update.get(symbol, 0)
            if now - last < self._orderbook_throttle_ms:
                return
            self._last_ob_update[symbol] = now

            # Для SHORT используем best bid как цену выхода
            current_price = order_book.get_best_bid()
            if not current_price:
                current_price = order_book.get_best_ask()

            if current_price:
                await position_manager.update_position_price(symbol, current_price)

                # Проверяем сигналы закрытия и усреднения
                await self._check_position_signals(symbol, current_price)

        except Exception as e:
            logger.error(f"Ошибка обработки обновления стакана {symbol}: {e}")

    async def _check_positions(self):
        """Проверить все открытые позиции"""
        try:
            # Периодическая синхронизация с биржей (каждые 30 сек)
            now = time.time()
            if now - self._last_exchange_sync >= 30:
                self._last_exchange_sync = now
                closed = await position_manager.detect_externally_closed()
                for symbol in closed:
                    # Сбрасываем в мониторинге для повторной обработки
                    listing_monitor.reset_symbol(symbol)
                    try:
                        await self.notifier.send_error(
                            f"🔄 Позиция {symbol} закрыта вне бота (вручную на бирже). "
                            f"Символ сброшен для повторного входа."
                        )
                    except Exception:
                        pass
                    # Отписываемся от WebSocket
                    if ws_client.is_connected():
                        await ws_client.unsubscribe_order_book(symbol)

            positions = position_manager.get_all_positions()

            for symbol, position in positions.items():
                current_price = float(position.current_price or position.entry_price)

                # Проверяем сигналы усреднения (при росте цены)
                avg_signal = position_manager.should_add_averaging(
                    symbol,
                    current_price,
                )

                if avg_signal:
                    await self._handle_averaging_signal(symbol, avg_signal, position)

        except Exception as e:
            logger.error(f"Ошибка проверки позиций: {e}")

    async def _check_position_signals(self, symbol: str, current_price: float):
        """
        Проверить сигналы закрытия и усреднения для позиции

        Args:
            symbol: Символ контракта
            current_price: Текущая цена
        """
        # Кулдаун после неудачной попытки закрытия (60 сек)
        if symbol in self._close_cooldowns:
            if time.time() - self._close_cooldowns[symbol] < 60:
                return
            del self._close_cooldowns[symbol]

        lock = await self._get_symbol_lock(symbol)
        async with lock:
            try:
                position = position_manager.get_position(symbol)
                if not position:
                    return

                # Проверяем сигналы закрытия (TP для SHORT)
                close_reason = position_manager.should_close_position(symbol, current_price)

                if close_reason:
                    success = await self._handle_close_signal(symbol, current_price, close_reason)
                    if not success:
                        self._close_cooldowns[symbol] = time.time()

            except Exception as e:
                logger.error(f"Ошибка проверки сигналов закрытия {symbol}: {e}")

    async def _handle_averaging_signal(self, symbol: str, signal, position):
        """
        Обработать сигнал усреднения

        Усреднение происходит при РОСТЕ цены от цены входа.
        Уровни: 300%, 700%, 1000%

        Args:
            symbol: Символ контракта
            signal: (avg_number, avg_level_pct)
            position: Позиция
        """
        try:
            avg_number, avg_level_pct = signal

            # Объём усреднения = объёму позиции (авто или ручной)
            volume_usdt = await self._calculate_position_size()

            # Получаем параметры контракта для корректной проверки маржи
            contract_info = await self.api_client.get_contract_info(symbol)
            ci_leverage = int(contract_info.get('leverage_max', 10) or 10) if contract_info else 10
            ci_maintenance = float(contract_info.get('maintenance_rate', 0.05) or 0.05) if contract_info else 0.05
            ci_taker_fee = float(contract_info.get('taker_fee_rate', 0.00075) or 0.00075) if contract_info else 0.00075

            # Проверяем риск
            can_trade, reason = await risk_manager.check_before_trade(
                volume_usdt, leverage=ci_leverage,
                maintenance_rate=ci_maintenance, taker_fee_rate=ci_taker_fee
            )

            if not can_trade:
                logger.warning(f"Нельзя усреднять {symbol}: {reason}")
                return

            # Добавляем усреднение
            success = await position_manager.add_averaging(
                symbol,
                float(position.current_price or position.entry_price),
                volume_usdt,
                avg_number,
                avg_level_pct,
            )

            if success:
                # Получаем обновлённую позицию с новой средней ценой
                updated_position = position_manager.get_position(symbol)
                new_avg_price = float(updated_position.entry_price) if updated_position else float(position.entry_price)
                logger.info(f"📊 Усреднение #{avg_number} добавлено: {symbol} на уровне {avg_level_pct}%")
                await self.notifier.send_averaging_added(
                    symbol,
                    avg_number,
                    float(position.current_price or position.entry_price),
                    volume_usdt,
                    new_avg_price,
                )

        except Exception as e:
            logger.error(f"Ошибка усреднения {symbol}: {e}")
            log_exception('trading', f"Ошибка усреднения {symbol}", e, symbol=symbol)
            try:
                await self.notifier.send_error(f"Ошибка усреднения {symbol}: {e}")
            except Exception:
                logger.error("Не удалось отправить уведомление об ошибке")

    async def _handle_close_signal(self, symbol: str, exit_price: float, reason: str) -> bool:
        """
        Обработать сигнал закрытия

        Для SHORT:
        - TP: цена упала на 2% (profit)
        - Timeout: позиция открыта слишком долго

        После закрытия проверяем возможность переоткрытия

        Args:
            symbol: Символ контракта
            exit_price: Цена выхода
            reason: Причина закрытия

        Returns:
            True если закрытие успешно
        """
        try:
            position = position_manager.get_position(symbol)
            if not position:
                return False

            # Закрываем позицию — возвращает реальную цену fill или None
            real_exit_price = await position_manager.close_position(
                symbol,
                exit_price,
                reason,
            )

            if real_exit_price is None:
                return False

            # Используем реальную цену fill для расчётов и уведомлений
            exit_price = real_exit_price

            # Вычисляем PnL для SHORT (используем Decimal для точности)
            d_entry = Decimal(str(position.entry_price))
            d_exit = Decimal(str(exit_price))
            d_volume = Decimal(str(position.total_volume_usdt))
            pnl_pct = float((d_entry - d_exit) / d_entry * 100)
            pnl_usdt = float(d_volume * (d_entry - d_exit) / d_entry)

            # Записываем PnL
            await risk_manager.record_trade_pnl(pnl_usdt)
            update_daily_pnl(
                pnl=pnl_usdt,
                volume_usdt=float(position.total_volume_usdt),
                is_winning=pnl_usdt >= 0,
            )

            # Вычисляем длительность позиции
            duration_str = ""
            if position.opened_at:
                delta = datetime.utcnow() - position.opened_at
                hours = int(delta.total_seconds() // 3600)
                minutes = int((delta.total_seconds() % 3600) // 60)
                duration_str = f"{hours}ч {minutes}мин"

            logger.info(
                f"🔴 SHORT закрыт: {symbol} @ ${exit_price:.6f} "
                f"PnL: ${pnl_usdt:+.2f} ({pnl_pct:+.2f}%) "
                f"[{reason}]"
            )

            await self.notifier.send_position_closed(
                symbol,
                exit_price,
                pnl_usdt,
                pnl_pct,
                reason,
                duration=duration_str,
                total_volume=float(position.total_volume_usdt),
            )

            # Обновляем режим разгона
            if reason == 'tp':
                acceleration_manager.on_tp_close(symbol)
            else:
                acceleration_manager.on_loss_close(symbol)

            # Проверяем возможность переоткрытия (только после TP)
            if reason == 'tp':
                await self._handle_reopen(symbol, exit_price)

            # Сбрасываем кулдаун при успехе
            self._close_cooldowns.pop(symbol, None)
            return True

        except Exception as e:
            logger.error(f"Ошибка закрытия позиции {symbol}: {e}")
            log_exception('trading', f"Ошибка закрытия позиции {symbol}", e, symbol=symbol)
            try:
                await self.notifier.send_error(f"Ошибка закрытия {symbol}: {e}")
            except Exception:
                logger.error("Не удалось отправить уведомление об ошибке")
            return False

    async def _handle_reopen(self, symbol: str, last_exit_price: float):
        """
        Обработать переоткрытие позиции после TP.
        Минимальная задержка: сразу после подтверждения закрытия отправляем ордер.

        Args:
            symbol: Символ контракта
            last_exit_price: Цена последнего выхода
        """
        try:
            # Защита от бесконечного цикла close/reopen при высокой волатильности
            now = time.time()
            max_reopens_per_window = 3
            window_seconds = 60

            window_start = self._reopen_window_start.get(symbol, 0)
            if now - window_start > window_seconds:
                self._reopen_counts[symbol] = 0
                self._reopen_window_start[symbol] = now

            self._reopen_counts[symbol] = self._reopen_counts.get(symbol, 0) + 1

            if self._reopen_counts[symbol] > max_reopens_per_window:
                logger.warning(
                    f"{symbol}: превышен лимит переоткрытий "
                    f"({max_reopens_per_window} за {window_seconds}с), пропускаем"
                )
                return

            # Проверяем текущую цену: если ушла >5% от exit — не переоткрываем
            ticker = await self.api_client.get_ticker(symbol)
            if ticker:
                current_price = float(ticker.get('last', 0) or 0)
                if current_price > 0 and last_exit_price > 0:
                    price_deviation_pct = abs(current_price - last_exit_price) / last_exit_price * 100
                    if price_deviation_pct > 5:
                        logger.warning(
                            f"{symbol}: цена ушла на {price_deviation_pct:.1f}% "
                            f"от exit (${last_exit_price:.6f} -> ${current_price:.6f}), "
                            f"переоткрытие отменено"
                        )
                        return

            # Получаем объём (авто или ручной) с учётом режима разгона
            base_volume = await self._calculate_position_size()
            volume_usdt = acceleration_manager.calculate_volume(symbol, base_volume)

            # Переоткрываем позицию сразу по цене последнего выхода
            # (reopen_position сам проверит ATH ratio, дни листинга, лимит позиций)
            position = await position_manager.reopen_position(
                symbol,
                last_exit_price,
                volume_usdt,
            )

            if position:
                real_entry = float(position.entry_price)
                logger.info(f"🔄 SHORT переоткрыт: {symbol} @ ${real_entry:.6f}")
                await self.notifier.send_position_reopened(
                    symbol,
                    real_entry,
                    float(position.total_volume_usdt),
                )

        except Exception as e:
            logger.error(f"Ошибка переоткрытия позиции {symbol}: {e}")

    def is_running(self) -> bool:
        """Проверить работает ли бот"""
        return self._running

    def get_status(self) -> dict:
        """Получить статус бота"""
        positions = position_manager.get_all_positions()
        risk_status = risk_manager.get_status()

        return {
            'running': self._running,
            'positions_count': len(positions),
            'symbols': list(positions.keys()),
            'daily_pnl': risk_status['daily_pnl'],
            'balance': risk_status['balance'],
            'circuit_breaker_state': risk_status['circuit_breaker_state'],
            'ws_connected': ws_client.is_connected(),
        }


# Глобальный инстанс (ленивая инициализация)
_bot_instance: Optional[TradingBot] = None


def get_trading_bot() -> Optional[TradingBot]:
    """Получить текущий инстанс торгового бота"""
    return _bot_instance


def set_trading_bot(bot: TradingBot):
    """Установить инстанс торгового бота"""
    global _bot_instance
    _bot_instance = bot
