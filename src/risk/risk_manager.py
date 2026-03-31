"""
Модуль риск-менеджмента
Проверка баланса, rate limiting, circuit breaker
"""
import asyncio
import time
import logging
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from collections import deque, defaultdict

from src.api.gate_client import GateApiClient
from src.db.connection import db
from src.utils.config import config

logger = logging.getLogger(__name__)


class BalanceChecker:
    """Проверка баланса счета"""

    def __init__(self):
        self.api_client = GateApiClient()
        self._last_balance: Optional[float] = None
        self._last_check_time: Optional[datetime] = None
        self._min_balance_threshold = 0.5  # Минимальный баланс в USDT

    async def get_balance(self) -> Optional[float]:
        """
        Получить текущий баланс фьючерсного счета

        Returns:
            Баланс в USDT или None
        """
        try:
            balance_data = await self.api_client.get_futures_balance()

            if not balance_data:
                logger.warning("Не получены данные о балансе")
                return None

            # Gate.io /futures/usdt/accounts возвращает dict с полем 'available'
            if isinstance(balance_data, dict):
                available = balance_data.get('available')
                if available is not None:
                    balance = float(available)
                    self._last_balance = balance
                    self._last_check_time = datetime.utcnow()
                    return balance
            # Fallback: если вернулся список (другие эндпоинты)
            elif isinstance(balance_data, list) and len(balance_data) > 0:
                usdt_balance = next(
                    (item for item in balance_data if item.get('currency') == 'USDT'),
                    None
                )
                if usdt_balance:
                    balance = float(usdt_balance.get('available', 0))
                    self._last_balance = balance
                    self._last_check_time = datetime.utcnow()
                    return balance

            logger.warning("Не найден баланс USDT")
            return None

        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            return None

    async def check_min_balance(self) -> bool:
        """
        Проверить что баланс выше минимального

        Returns:
            True если баланс достаточный
        """
        balance = await self.get_balance()

        if balance is None:
            logger.error("Не удалось получить баланс")
            return False

        if balance < self._min_balance_threshold:
            logger.warning(
                f"Баланс слишком низкий: ${balance:.2f} < ${self._min_balance_threshold:.2f}"
            )
            return False

        return True

    async def can_afford_position(
        self, volume_usdt: float, leverage: int = 10,
        maintenance_rate: float = 0.05, taker_fee_rate: float = 0.00075
    ) -> bool:
        """
        Проверить что хватает средств на позицию (с учётом плеча)

        Args:
            volume_usdt: Объем позиции в USDT (номинал)
            leverage: Реальное плечо контракта
            maintenance_rate: Ставка поддерживающей маржи (из contract_info)
            taker_fee_rate: Комиссия тейкера (из contract_info)

        Returns:
            True если достаточно средств
        """
        balance = await self.get_balance()

        if balance is None:
            return False

        # Формула Gate.io: маржа = номинал * (1/leverage + maintenance_rate + 2*taker_fee)
        margin_rate = (1.0 / leverage) + maintenance_rate + 2 * taker_fee_rate
        required_margin = volume_usdt * margin_rate

        if balance < required_margin:
            logger.warning(
                f"Недостаточно средств: маржа ${required_margin:.2f} "
                f"(x{leverage}, maint={maintenance_rate}), есть ${balance:.2f}"
            )
            return False

        return True

    def set_min_balance_threshold(self, threshold: float):
        """Установить минимальный порог баланса"""
        self._min_balance_threshold = threshold
        logger.info(f"Минимальный баланс установлен: ${threshold:.2f}")


class RateLimiter:
    """
    Ограничитель частоты запросов (Rate Limiter)
    Защита от превышения лимитов API
    """

    def __init__(self, max_requests_per_second: int = 10):
        self.max_requests_per_second = max_requests_per_second
        self._requests = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Получить разрешение на запрос"""
        while True:
            sleep_time = 0.0
            async with self._lock:
                now = time.time()

                # Удаляем старые запросы (старше 1 секунды)
                while self._requests and self._requests[0] < now - 1:
                    self._requests.popleft()

                # Проверяем лимит
                if len(self._requests) >= self.max_requests_per_second:
                    # Вычисляем время ожидания
                    sleep_time = self._requests[0] + 1 - now
                else:
                    # Лимит не превышен — добавляем запрос и выходим
                    self._requests.append(time.time())
                    return

            # Спим ВОВНЕ lock, чтобы не блокировать другие корутины
            if sleep_time > 0:
                logger.debug(f"Rate limit: ждем {sleep_time:.2f}с")
                await asyncio.sleep(sleep_time)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        pass


class CircuitBreaker:
    """
    Предохранитель (Circuit Breaker)
    Автоматическое отключение при ошибках
    """

    # Состояния предохранителя
    CLOSED = 'closed'  # Нормально работает
    OPEN = 'open'  # Отключен из-за ошибок
    HALF_OPEN = 'half_open'  # Проверка восстановления

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: Exception = Exception,
    ):
        """
        Args:
            failure_threshold: Количество ошибок для открытия
            recovery_timeout: Время ожидания перед попыткой восстановления (сек)
            expected_exception: Тип исключения для отслеживания
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception

        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._state = self.CLOSED

    async def call(self, func, *args, **kwargs):
        """
        Вызвать функцию через предохранитель

        Args:
            func: Функция для вызова
            *args: Аргументы функции
            **kwargs: Именованные аргументы

        Returns:
            Результат функции

        Raises:
            Exception: Если предохранитель открыт
        """
        if self._state == self.OPEN:
            if not self._should_attempt_reset():
                raise CircuitBreakerOpenException(
                    f"Предохранитель открыт. Попробуйте через {self._get_remaining_time()}с"
                )
            self._state = self.HALF_OPEN
            logger.info("Предохранитель: переход в HALF_OPEN")

        try:
            result = await func(*args, **kwargs)

            # Успешный вызов
            if self._state == self.HALF_OPEN:
                self._reset()
                logger.info("Предохранитель: восстановлен в CLOSED")

            return result

        except self.expected_exception as e:
            self._on_failure()
            raise

    def _should_attempt_reset(self) -> bool:
        """Проверить время восстановления"""
        return (
            self._last_failure_time and
            time.time() - self._last_failure_time >= self.recovery_timeout
        )

    def _get_remaining_time(self) -> float:
        """Получить оставшееся время восстановления"""
        if not self._last_failure_time:
            return 0.0

        elapsed = time.time() - self._last_failure_time
        remaining = self.recovery_timeout - elapsed
        return max(0.0, remaining)

    def _on_failure(self):
        """Обработка ошибки"""
        self._failure_count += 1
        self._last_failure_time = time.time()

        logger.error(
            f"Предохранитель: ошибка {self._failure_count}/{self.failure_threshold}"
        )

        if self._failure_count >= self.failure_threshold:
            self._state = self.OPEN
            logger.error(
                f"Предохранитель: ОТКРЫТ на {self.recovery_timeout}с"
            )

    def _reset(self):
        """Сброс предохранителя"""
        self._failure_count = 0
        self._last_failure_time = None
        self._state = self.CLOSED

    def get_state(self) -> str:
        """Получить текущее состояние"""
        return self._state

    def get_failure_count(self) -> int:
        """Получить количество ошибок"""
        return self._failure_count


class CircuitBreakerOpenException(Exception):
    """Исключение когда предохранитель открыт"""
    pass


class RiskManager:
    """Менеджер риск-менеджмента"""

    def __init__(self):
        self.balance_checker = BalanceChecker()
        self.rate_limiter = RateLimiter(
            max_requests_per_second=config.risk.max_open_orders_per_second
        )
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=config.risk.circuit_breaker_errors,
            recovery_timeout=60,
        )
        self._daily_pnl = 0.0
        self._initial_balance: Optional[float] = None  # Баланс на начало дня
        self._last_reset_date = datetime.utcnow().date()

    def _get_daily_loss_limit_from_db(self) -> float:
        """Получить дневной лимит просадки из настроек БД (перечитывает каждый раз)"""
        if not db._initialized:
            logger.debug("БД не инициализирована, использую значение по умолчанию для daily_loss_limit")
            return 1000.0

        try:
            from src.db.settings import SettingsManager
            with db.get_session() as session:
                settings = SettingsManager(session)
                max_drawdown_pct = settings.get('max_drawdown_pct', 50)
                # Используем initial_balance (баланс на начало дня), а не текущий
                balance = self._initial_balance or self.balance_checker._last_balance
                if balance and balance > 0:
                    return balance * (max_drawdown_pct / 100)
                return 1000.0
        except Exception as e:
            logger.warning(f"Ошибка получения daily_loss_limit из БД: {e}, использую значение по умолчанию")
            return 1000.0

    async def check_before_trade(
        self, volume_usdt: float, leverage: int = 10,
        maintenance_rate: float = 0.05, taker_fee_rate: float = 0.00075
    ) -> tuple[bool, str]:
        """
        Проверка перед торговлей

        Args:
            volume_usdt: Объем позиции в USDT
            leverage: Реальное плечо контракта
            maintenance_rate: Ставка поддерживающей маржи
            taker_fee_rate: Комиссия тейкера

        Returns:
            (can_trade, reason) - Можно торговать и причина если нет
        """
        # Проверяем баланс
        if not await self.balance_checker.check_min_balance():
            return False, "Баланс слишком низкий"

        # Проверяем достаточно ли средств
        if not await self.balance_checker.can_afford_position(
            volume_usdt, leverage=leverage,
            maintenance_rate=maintenance_rate, taker_fee_rate=taker_fee_rate
        ):
            return False, "Недостаточно средств для позиции"

        # Проверяем дневной лимит потерь
        if not await self._check_daily_loss_limit():
            return False, "Достигнут дневной лимит потерь"

        # Проверяем состояние предохранителя
        if self.circuit_breaker.get_state() == CircuitBreaker.OPEN:
            return False, "Предохранитель открыт из-за ошибок"

        return True, "OK"

    async def _check_daily_loss_limit(self) -> bool:
        """Проверить дневной лимит потерь"""
        today = datetime.utcnow().date()
        if today != self._last_reset_date:
            self._last_reset_date = today
            # Сохраняем баланс на начало дня
            balance = await self.balance_checker.get_balance()
            if balance:
                self._initial_balance = balance
            # Загружаем PnL из trades за сегодня (переживает рестарт)
            self._daily_pnl = self._load_daily_pnl_from_db()
            logger.info(f"Новый день, PnL из БД: ${self._daily_pnl:.2f}, initial_balance: ${self._initial_balance or 0:.2f}")
            # Сохраняем баланс на начало дня в pnl_history
            try:
                from src.db.pnl_tracker import save_daily_balance
                save_daily_balance(balance_start=self._initial_balance)
            except Exception:
                pass

        daily_loss_limit = self._get_daily_loss_limit_from_db()
        if self._daily_pnl < -daily_loss_limit:
            logger.error(
                f"Достигнут лимит потерь: ${self._daily_pnl:.2f} < -${daily_loss_limit:.2f}"
            )
            # Ставим max_concurrent_coins=0 — остановка торговли (согласно ТЗ F4.2)
            self._stop_trading_on_drawdown()
            return False

        return True

    def _load_daily_pnl_from_db(self) -> float:
        """Загрузить PnL за сегодня из таблицы trades (переживает рестарт)"""
        if not db._initialized:
            return 0.0
        try:
            from src.db.models import Trade
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            with db.get_session() as session:
                trades = session.query(Trade).filter(
                    Trade.created_at >= today_start,
                    Trade.pnl.isnot(None)
                ).all()
                return sum(float(t.pnl) for t in trades)
        except Exception as e:
            logger.warning(f"Ошибка загрузки PnL из БД: {e}")
            return 0.0

    def _stop_trading_on_drawdown(self):
        """Остановить торговлю при превышении лимита просадки"""
        if not db._initialized:
            return
        try:
            from src.db.settings import SettingsManager
            with db.get_session() as session:
                settings = SettingsManager(session)
                current = settings.get('max_concurrent_coins', 10)
                if current != 0:
                    settings.set('max_concurrent_coins', 0, updated_by='system')
                    logger.error("ТОРГОВЛЯ ОСТАНОВЛЕНА: max_concurrent_coins = 0 (дневной лимит просадки)")
        except Exception as e:
            logger.error(f"Ошибка остановки торговли: {e}")

    async def record_trade_pnl(self, pnl: float):
        """
        Записать PnL от торговли

        Args:
            pnl: Прибыль/убыток в USDT
        """
        self._daily_pnl += pnl

        logger.info(
            f"PnL сделки: ${pnl:+.2f}, за день: ${self._daily_pnl:+.2f}"
        )

        # Если большой убыток - проверяем лимит
        if pnl < 0:
            await self._check_daily_loss_limit()

    async def execute_with_protection(self, func, *args, **kwargs):
        """
        Выполнить функцию с защитой (rate limiter + circuit breaker)

        Args:
            func: Функция для выполнения
            *args: Аргументы
            **kwargs: Именованные аргументы

        Returns:
            Результат функции
        """
        async with self.rate_limiter:
            return await self.circuit_breaker.call(func, *args, **kwargs)

    def get_daily_pnl(self) -> float:
        """Получить PnL за день"""
        return self._daily_pnl

    def get_status(self) -> Dict[str, any]:
        """Получить статус риск-менеджмента"""
        return {
            'balance': self.balance_checker._last_balance,
            'balance_last_check': self.balance_checker._last_check_time,
            'daily_pnl': self._daily_pnl,
            'daily_loss_limit': self._get_daily_loss_limit_from_db(),  # Получаем из БД
            'circuit_breaker_state': self.circuit_breaker.get_state(),
            'circuit_breaker_failures': self.circuit_breaker.get_failure_count(),
            'rate_limit': self.rate_limiter.max_requests_per_second,
        }


# Глобальный инстанс
risk_manager = RiskManager()


class BalanceProtectionChecker:
    """
    Checker баланса для защиты от ликвидации
    Отдельный процесс с максимальными правами API
    
    Логика работы:
    1. Каждые 5 секунд запрашивает позиции и баланс
    2. Рассчитывает unrealized PnL по всем позициям
    3. Если просадка >= protection_trigger_pct (50%) от total_balance:
       - Переводит protection_transfer_pct (25%) от total_balance со спота на фьючерсы
    """

    def __init__(self):
        self.api_client = GateApiClient()
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Используем значения по умолчанию, чтобы не обращаться к БД при инициализации
        # Настройки будут загружены из БД при необходимости
        self._protection_trigger_pct = 50.0
        self._protection_transfer_pct = 25.0
        self._settings_loaded = False

    def _get_protection_settings(self, param_name: str, default: float) -> float:
        """Получить настройку защиты из БД"""
        # Проверяем, инициализирована ли БД
        if not db._initialized:
            logger.debug(f"БД не инициализирована, использую значение по умолчанию для {param_name}: {default}")
            return default

        try:
            from src.db.settings import SettingsManager
            with db.get_session() as session:
                settings = SettingsManager(session)
                return settings.get(param_name, default)
        except Exception as e:
            logger.warning(f"Ошибка получения настройки {param_name} из БД: {e}, использую {default}")
            return default

    async def load_settings_from_db(self):
        """Загрузить настройки защиты из БД (вызывать после init_db)"""
        if not db._initialized:
            logger.debug("БД не инициализирована, пропускаем загрузку настроек")
            return

        try:
            from src.db.settings import SettingsManager
            with db.get_session() as session:
                settings = SettingsManager(session)
                self._protection_trigger_pct = settings.get('protection_trigger_pct', 50.0)
                self._protection_transfer_pct = settings.get('protection_transfer_pct', 25.0)
                self._settings_loaded = True
                logger.info(f"Настройки защиты загружены: trigger={self._protection_trigger_pct}%, transfer={self._protection_transfer_pct}%")
        except Exception as e:
            logger.warning(f"Ошибка загрузки настроек из БД: {e}, используем значения по умолчанию")

    async def start(self):
        """Запустить checker"""
        if self._running:
            logger.warning("BalanceProtectionChecker уже запущен")
            return

        # Загружаем настройки из БД при старте
        await self.load_settings_from_db()

        self._running = True
        self._task = asyncio.create_task(self._protection_loop())
        logger.info(f"BalanceProtectionChecker запущен (trigger: {self._protection_trigger_pct}%, transfer: {self._protection_transfer_pct}%)")

    async def stop(self):
        """Остановить checker"""
        if not self._running:
            return

        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        await self.api_client.close()
        logger.info("BalanceProtectionChecker остановлен")

    async def _protection_loop(self):
        """Главный цикл защиты"""
        while self._running:
            try:
                await self._check_and_protect()
                await asyncio.sleep(5)  # Проверяем каждые 5 секунд
            except asyncio.CancelledError:
                logger.info("BalanceProtectionChecker цикл отменен")
                break
            except Exception as e:
                logger.error(f"Ошибка в BalanceProtectionChecker: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _check_and_protect(self):
        """Проверить просадку и защитить при необходимости"""
        try:
            # Получаем unrealized PnL по всем позициям
            unrealized_pnl = await self._get_total_unrealized_pnl()

            if unrealized_pnl is None:
                return

            # Получаем балансы
            futures_balance = await self._get_futures_balance()
            spot_balance = await self._get_spot_balance()

            if futures_balance is None or spot_balance is None:
                return

            total_balance = futures_balance + spot_balance

            if total_balance <= 0:
                return

            # Проверяем просадку
            # unrealized_pnl отрицательный при убытке
            if unrealized_pnl < 0:
                unrealized_pnl_abs = abs(unrealized_pnl)
                drawdown_pct = (unrealized_pnl_abs / total_balance) * 100

                if drawdown_pct >= self._protection_trigger_pct:
                    logger.warning(
                        f"💰 PROTECTION TRIGGERED! "
                        f"Просадка: {drawdown_pct:.1f}% >= {self._protection_trigger_pct}%"
                    )

                    # Рассчитываем сумму для перевода
                    transfer_amount = total_balance * (self._protection_transfer_pct / 100)

                    # Переводим со спота на фьючерсы
                    await self._transfer_spot_to_futures(transfer_amount)

        except Exception as e:
            logger.error(f"Ошибка проверки защиты: {e}")

    async def _get_total_unrealized_pnl(self) -> Optional[float]:
        """Получить общий unrealized PnL по всем позициям"""
        try:
            # Получаем сессию API клиента
            session = await self.api_client.get_session()
            url_path = '/futures/usdt/positions'
            url = self.api_client._build_url(url_path)

            # Используем авторизованные заголовки
            headers = self.api_client._get_auth_headers('GET', url_path)

            async with session.get(url, headers=headers) as response:
                if response is None or not hasattr(response, 'status'):
                    logger.debug("API вернул None для позиций")
                    return 0.0

                if response.status != 200:
                    logger.debug(f"API вернул статус {response.status} для позиций")
                    return 0.0

                positions = await response.json()

                if positions is None:
                    return 0.0

                total_pnl = 0.0
                for pos in positions:
                    if pos is None:
                        continue
                    # unrealized_pnl уже в USDT
                    pnl = float(pos.get('unrealized_pnl', 0) or 0)
                    total_pnl += pnl

                return total_pnl

        except Exception as e:
            logger.debug(f"Ошибка получения unrealized PnL: {e}")
            return 0.0  # Возвращаем 0 вместо None

    async def _get_futures_balance(self) -> Optional[float]:
        """Получить баланс фьючерсов"""
        try:
            balance_data = await self.api_client.get_futures_balance()

            if not balance_data:
                return None

            # Gate.io /futures/usdt/accounts возвращает dict с полем 'available'
            if isinstance(balance_data, dict):
                available = balance_data.get('available')
                if available is not None:
                    return float(available)
            elif isinstance(balance_data, list) and len(balance_data) > 0:
                usdt_balance = next(
                    (item for item in balance_data if item.get('currency') == 'USDT'),
                    None
                )
                if usdt_balance:
                    return float(usdt_balance.get('available', 0))

            return None

        except Exception as e:
            logger.error(f"Ошибка получения баланса фьючерсов: {e}")
            return None

    async def _get_spot_balance(self) -> Optional[float]:
        """Получить баланс спота"""
        try:
            balance_data = await self.api_client.get_spot_balance()

            if not balance_data:
                return None

            # Извлекаем баланс USDT
            if isinstance(balance_data, list):
                usdt_balance = next(
                    (item for item in balance_data if item.get('currency') == 'USDT'),
                    None
                )
                if usdt_balance:
                    return float(usdt_balance.get('available', 0))

            return None

        except Exception as e:
            logger.error(f"Ошибка получения баланса спота: {e}")
            return None

    async def _transfer_spot_to_futures(self, amount: float):
        """
        Перевести USDT со спота на фьючерсы через Gate.io /wallet/transfers API.

        Args:
            amount: Сумма для перевода
        """
        try:
            logger.info(f"💰 Перевод ${amount:.2f} USDT: Спот → Фьючерсы")

            # Проверяем спотовый баланс перед переводом
            spot_balance = await self._get_spot_balance()
            if spot_balance is None or spot_balance <= 0:
                logger.warning("Спотовый баланс пуст или недоступен, перевод невозможен")
                return
            if amount > spot_balance:
                logger.warning(f"Запрошено ${amount:.2f}, но на споте только ${spot_balance:.2f}. Переводим доступное.")
                amount = spot_balance

            if amount < 0.01:
                logger.warning(f"Сумма перевода слишком мала: ${amount:.2f}")
                return

            # Правильный endpoint Gate.io v4: /wallet/transfers
            url_path = '/wallet/transfers'
            url = self.api_client._build_url(url_path)

            payload = {
                'currency': 'USDT',
                'from': 'spot',
                'to': 'futures',
                'amount': str(round(amount, 2)),
                'settle': 'usdt',
            }

            import json
            body = json.dumps(payload)
            headers = self.api_client._get_auth_headers('POST', url_path, body=body)

            session = await self.api_client.get_session()
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status not in (200, 201):
                    error_text = await response.text()
                    logger.error(f"Ошибка перевода: {response.status} {error_text[:300]}")
                    return

                logger.info(f"✅ Перевод выполнен успешно: ${amount:.2f} USDT (Спот → Фьючерсы)")

                # Уведомляем в Telegram
                try:
                    from src.telegram.bot import get_notifier
                    notifier = get_notifier()
                    await notifier.send_balance_transfer(
                        from_account='spot',
                        to_account='futures',
                        amount=amount,
                        reason=f'Просадка {self._protection_trigger_pct}%'
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления: {e}")

        except Exception as e:
            logger.error(f"Ошибка выполнения перевода: {e}")


# Глобальный инстанс для защиты баланса
balance_protection_checker = BalanceProtectionChecker()
