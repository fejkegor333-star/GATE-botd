"""
Модуль мониторинга новых листингов
Периодически опрашивает API и сохраняет новые контракты в БД
"""
import asyncio
import logging
import time
from typing import Dict, List, Optional, Set
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from src.api.gate_client import GateApiClient
from src.db.connection import db
from src.db.models import Contract
from src.db.settings import SettingsManager
from src.utils.config import config

logger = logging.getLogger(__name__)

# Стейблкоины — не торгуем (нет волатильности)
STABLECOINS = {
    'USDC', 'BUSD', 'DAI', 'TUSD', 'FDUSD', 'USDD', 'PYUSD', 'USDP',
    'GUSD', 'FRAX', 'LUSD', 'CEUR', 'SUSD', 'MIM', 'UST', 'USTC',
    'EURC', 'EURT', 'USDJ', 'CUSD', 'USDN', 'USDK', 'HUSD', 'TRIBE',
}

# Токенизированные акции — не подходят под стратегию SHORT на новых листингах
STOCK_TOKENS = {
    'AAPL', 'TSLA', 'AMZN', 'GOOGL', 'META', 'MSFT', 'NVDA', 'NFLX',
    'COIN', 'MSTR', 'GME', 'AMC', 'BABA', 'AMD', 'INTC', 'PYPL',
    'SQ', 'UBER', 'ABNB', 'SNAP', 'PLTR',
}


def _is_filtered_symbol(symbol: str) -> str | None:
    """
    Проверить, нужно ли отфильтровать символ (стейблкоин или акция).

    Returns:
        Причина фильтрации или None если символ допустим.
    """
    # Извлекаем base currency: "USDC_USDT" -> "USDC"
    base = symbol.split('_')[0] if '_' in symbol else symbol

    if base in STABLECOINS:
        return f'stablecoin ({base})'
    if base in STOCK_TOKENS:
        return f'stock token ({base})'
    return None


class ListingMonitor:
    """Монитор новых листингов"""

    def __init__(self):
        self.api_client = GateApiClient()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._known_symbols: Set[str] = set()
        self._processing_symbols: Set[str] = set()  # Символы в процессе обработки
        self._on_new_listing_callbacks: List = []
        self._retry_after: Dict[str, float] = {}  # symbol -> timestamp когда можно повторить

    def on_new_listing(self, callback):
        """Зарегистрировать callback на новый листинг: async def callback(symbol, contract_data)"""
        self._on_new_listing_callbacks.append(callback)

    async def start(self):
        """Запустить мониторинг"""
        if self._running:
            logger.warning("Мониторинг уже запущен")
            return

        self._running = True

        # Загружаем известные символы из БД
        await self._load_known_symbols()

        # Запускаем задачу мониторинга
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("Мониторинг новых листингов запущен")

    async def stop(self):
        """Остановить мониторинг"""
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
        logger.info("Мониторинг новых листингов остановлен")

    def mark_listing_processed(self, symbol: str):
        """Пометить листинг как успешно обработанный (позиция открыта)"""
        self._processing_symbols.discard(symbol)
        self._known_symbols.add(symbol)
        self._retry_after.pop(symbol, None)

    def reset_symbol(self, symbol: str):
        """Сбросить символ для повторной обработки (после внешнего закрытия)"""
        self._known_symbols.discard(symbol)
        self._processing_symbols.discard(symbol)
        self._retry_after.pop(symbol, None)
        logger.info(f"Символ {symbol} сброшен для повторной обработки")

    def mark_listing_failed(self, symbol: str, permanent: bool = False, retry_minutes: int = 5):
        """Пометить что обработка листинга не удалась.

        Args:
            symbol: Символ контракта
            permanent: Если True — не повторять.
                       Если False — повторим через retry_minutes минут.
            retry_minutes: Через сколько минут повторить (по умолчанию 5).
        """
        self._processing_symbols.discard(symbol)
        if permanent:
            self._known_symbols.add(symbol)
            self._retry_after.pop(symbol, None)
            logger.info(f"Контракт {symbol} помечен как окончательно неудачный, повтор не будет")
        else:
            self._retry_after[symbol] = time.time() + retry_minutes * 60
            logger.info(f"Контракт {symbol}: повтор через {retry_minutes} мин")

    def _get_listing_days_limit(self) -> int:
        """Получить лимит дней с листинга из настроек БД"""
        try:
            with db.get_session() as session:
                settings = SettingsManager(session)
                return int(settings.get('days_since_listing_limit', 30))
        except Exception:
            return 30

    async def _load_known_symbols(self):
        """Загрузить известные символы из БД"""
        days_limit = self._get_listing_days_limit()

        with db.get_session() as session:
            contracts = session.query(Contract).all()
            self._known_symbols = set()
            pending_count = 0

            for c in contracts:
                # Если контракт не был обработан и свежий — НЕ добавляем в known,
                # чтобы мониторинг подобрал его заново
                if (not c.listing_taken_in_work
                        and c.first_seen_at
                        and c.first_seen_at > datetime.utcnow() - timedelta(days=days_limit)):
                    pending_count += 1
                    continue

                self._known_symbols.add(c.symbol)

            logger.info(f"Загружено {len(self._known_symbols)} обработанных контрактов, {pending_count} ожидают повторной обработки (лимит: {days_limit} дней)")

    async def _monitor_loop(self):
        """Главный цикл мониторинга"""
        while self._running:
            try:
                await self._check_listings()

                # Ждем перед следующим опросом
                await asyncio.sleep(config.monitoring.poll_interval_seconds)

            except asyncio.CancelledError:
                logger.info("Цикл мониторинга отменен")
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}", exc_info=True)
                # Пауза перед повторной попыткой
                await asyncio.sleep(10)

    async def _check_listings(self):
        """Проверить новые листинги"""
        try:
            # Получаем контракты из API
            contracts_data = await self.api_client.fetch_contracts()

            if not contracts_data:
                logger.warning("Не получено данных о контрактах")
                return

            # Логируем успешную проверку
            api_count = len(contracts_data)
            known_count = len(self._known_symbols)
            diff = api_count - known_count
            if diff > 0:
                logger.info(f"📡 Проверка контрактов: API={api_count}, известных={known_count}, новых кандидатов={diff}")
            else:
                logger.info(f"📡 Проверка контрактов: {api_count} контрактов, новых нет")

            # Фильтруем новые
            new_contracts = []

            for contract_data in contracts_data:
                symbol = contract_data.get('name', '')  # Gate использует 'name' как symbol

                if not symbol:
                    continue

                # Если контракт уже известен или в обработке - пропускаем
                if symbol in self._known_symbols or symbol in self._processing_symbols:
                    continue

                # Если символ в cooldown после неудачной попытки - пропускаем
                if symbol in self._retry_after:
                    if time.time() < self._retry_after[symbol]:
                        continue
                    # Cooldown истёк — убираем и пробуем снова
                    del self._retry_after[symbol]

                # Фильтруем стейблкоины и токенизированные акции
                filter_reason = _is_filtered_symbol(symbol)
                if filter_reason:
                    logger.info(f"Контракт {symbol} отфильтрован: {filter_reason}")
                    self._known_symbols.add(symbol)
                    continue

                # Проверяем create_time (время создания контракта)
                # launch_time в Gate.io API - это время экспирации, а не листинга!
                create_time = contract_data.get('create_time')
                if not create_time:
                    # Если create_time нет, пропускаем
                    logger.debug(f"Контракт {symbol} не имеет create_time, пропускаем")
                    self._known_symbols.add(symbol)
                    continue

                try:
                    # create_time приходит как Unix timestamp (секунды)
                    if isinstance(create_time, (int, float)):
                        launch_time = datetime.utcfromtimestamp(create_time)
                    else:
                        # Пробуем распарсить как строку
                        launch_time = datetime.fromisoformat(create_time.replace('Z', '+00:00'))
                        launch_time = launch_time.replace(tzinfo=None)

                    # Проверяем, что это новый листинг (не старше days_since_listing_limit)
                    days_limit = self._get_listing_days_limit()
                    if launch_time < datetime.utcnow() - timedelta(days=days_limit):
                        logger.debug(f"Контракт {symbol} слишком старый ({launch_time}, лимит {days_limit} дней), пропускаем")
                        # Все равно добавляем в known_symbols чтобы больше не проверять
                        self._known_symbols.add(symbol)
                        continue

                    # Новый листинг!
                    new_contracts.append({
                        'symbol': symbol,
                        'launch_time': launch_time,
                        'data': contract_data,
                    })

                    logger.info(f"🚀 Обнаружен новый листинг: {symbol} (create_time: {launch_time})")

                except (ValueError, OSError) as e:
                    logger.error(f"Ошибка парсинга времени для {symbol}: {e}")
                    continue

            # Сохраняем новые контракты в БД и уведомляем
            if new_contracts:
                symbols_to_process = await self._save_new_contracts(new_contracts)

                # Уведомляем о новых листингах (неблокирующе через create_task)
                for contract_info in new_contracts:
                    if contract_info['symbol'] not in symbols_to_process:
                        continue
                    for callback in self._on_new_listing_callbacks:
                        try:
                            asyncio.create_task(callback(contract_info['symbol'], contract_info['data']))
                        except Exception as cb_err:
                            logger.error(f"Ошибка в callback нового листинга: {cb_err}")

        except Exception as e:
            logger.error(f"Ошибка при проверке листингов: {e}", exc_info=True)

    async def _save_new_contracts(self, new_contracts: List[dict]) -> Set[str]:
        """Сохранить новые контракты в БД. Возвращает набор символов для обработки."""
        symbols_to_process = set()
        already_done = []

        with db.get_session() as session:
            for contract_info in new_contracts:
                symbol = contract_info['symbol']
                launch_time = contract_info['launch_time']

                # Проверяем, что еще не создан (могли создать параллельно)
                existing = session.query(Contract).filter(
                    Contract.symbol == symbol
                ).first()

                if existing:
                    if existing.listing_taken_in_work:
                        logger.info(f"Контракт {symbol} уже обработан")
                        already_done.append(symbol)
                    else:
                        # Не обработан — нужно повторить
                        logger.info(f"🔄 Контракт {symbol} ещё не обработан, повторяем")
                        symbols_to_process.add(symbol)
                    continue

                # Создаем новую запись (listing_taken_in_work=False — ещё не торговали)
                contract = Contract(
                    symbol=symbol,
                    launch_time=launch_time,
                    status='new',
                    first_seen_at=datetime.utcnow(),
                    listing_taken_in_work=False,
                )

                session.add(contract)
                logger.info(f"Сохранен новый контракт: {symbol}")

            try:
                session.commit()
                # Коммит успешен — новые контракты нужно обработать
                for contract_info in new_contracts:
                    s = contract_info['symbol']
                    if s not in already_done:
                        symbols_to_process.add(s)
                # Помечаем уже обработанные как известные
                for s in already_done:
                    self._known_symbols.add(s)
                # Добавляем новые в _processing_symbols (не в _known!)
                for s in symbols_to_process:
                    self._processing_symbols.add(s)
                logger.info(f"Контрактов к обработке: {len(symbols_to_process)}")
            except Exception as e:
                session.rollback()
                logger.error(f"Ошибка при сохранении контрактов: {e}")
                # НЕ добавляем в _known_symbols — повторим на следующем цикле
                symbols_to_process.clear()

        return symbols_to_process

    async def update_ath(self, symbol: str) -> Optional[float]:
        """
        Обновить ATH для контракта

        Args:
            symbol: Символ контракта

        Returns:
            ATH цена или None если ошибка
        """
        try:
            # Получаем недельные свечи
            candles = await self.api_client.fetch_candles(
                contract=symbol,
                interval='1w',
                limit=config.ath.lookback_weeks,  # ~10 лет
            )

            if not candles:
                logger.warning(f"Не получены свечи для {symbol}")
                return None

            # Ищем максимум (high - индекс 2)
            ath = max(float(candle[2]) for candle in candles if candle[2])

            # Обновляем в БД
            with db.get_session() as session:
                contract = session.query(Contract).filter(
                    Contract.symbol == symbol
                ).first()

                if contract:
                    contract.ath_price = ath
                    contract.ath_updated_at = datetime.utcnow()
                    session.commit()
                    logger.info(f"ATH обновлен для {symbol}: ${ath}")
                else:
                    logger.warning(f"Контракт {symbol} не найден в БД")

            return ath

        except Exception as e:
            logger.error(f"Ошибка при обновлении ATH для {symbol}: {e}")
            return None


# Глобальный инстанс
monitor = ListingMonitor()
