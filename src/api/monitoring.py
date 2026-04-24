"""
Модуль мониторинга новых листингов
Периодически опрашивает API и сохраняет новые контракты в БД
"""
import asyncio
import logging
import re
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
    'USD1', 'USDY', 'USDX', 'USDE', 'RLUSD', 'EURI', 'USDB', 'USDQ',
    'GHO', 'CRVUSD', 'OUSD', 'SDAI', 'YUSD', 'XUSD',
}

# Не-крипто типы контрактов Gate.io (поле contract_type из API)
NON_CRYPTO_CONTRACT_TYPES = {'stocks', 'indices', 'metals', 'commodities', 'forex'}

# Минимальный блэклист — только те символы, которые мы РЕАЛЬНО видели как акции
# с пустым contract_type на Gate.io. Не добавляем тикеры, которые могут быть крипто-токенами!
#
# Главная защита — contract_type + is_pre_market из API.
# Этот блэклист — резерв для случаев когда Gate.io вообще не проставил тип
# (наблюдалось для китайских акций GEELY, KUAISHOU, ZHIPU, XUNCE).
#
# ВАЖНО: НЕ добавлять короткие/общие тикеры, т.к. многие из них реальная крипта:
# DASH (Dash coin), CVX (Convex), DIA (DIA Oracle), GAS (Neo Gas), CAT (мем),
# COIN (может быть мем), NET (может быть крипто), DASH, MSTR (может быть мем),
# BP (мы видели как pre_market stock — поймает is_pre_market), CFG (Centrifuge крипто),
# UP (мем), F, T, V, MA, GE, JD, LI и т.д.
KNOWN_STOCK_TICKERS = {
    # Китайские акции, которые реально проскочили (Gate.io не проставил contract_type)
    'GEELY', 'KUAISHOU', 'ZHIPU', 'XUNCE', 'XIAOMI', 'BABA', 'KWEB',
    # SPACEX — мы видели как акция (потеряли $8 на ней)
    'SPACEX',
    # Акции с пустым contract_type, реально проскочили 2026-04-24
    'FSLR', 'BWXT',
    # Акции/ETF из Gate.io с contract_type="stocks" (резерв на случай сброса типа)
    'INTC', 'CSCO', 'IBM', 'JPM', 'TSM', 'AMD', 'AVGO', 'MSFT', 'ASML',
    'LLY', 'UNH', 'MCD', 'LMT', 'ACN', 'MRVL', 'MU',
    'ARM', 'CCJ', 'COHR', 'CEG', 'CARR', 'LITE', 'JDON', 'WDC',
    'ANTA', 'SUNAC', 'CITIC', 'MEITUAN', 'LENOVO', 'TENCENT', 'AKESO',
    'OPENAI', 'ANTHROPIC', 'ANDURIL', 'KALSHI', 'POLYMARKET', 'MINIMAX',
    'DEEPTECH', 'FUTUON', 'RDDTON', 'BTGOON',
    'GE', 'GD', 'BA', 'NOC', 'RTX', 'BE',
    'PEP', 'KO', 'PG', 'WMT', 'COST',
    'AGG', 'TLT', 'IEFA', 'EWJ', 'EWT', 'EWY',
    'SNDK', 'SBP', 'VCX', 'BMNR', 'PAYP',
    # X-варианты явных акций (поймаются также паттерном, дублируем для надёжности)
    'TSLAX', 'MSTRX', 'NVDAX', 'METAX', 'GOOGLX', 'AAPLX', 'MSFTX', 'COINX',
    'AMDX', 'AMZNX', 'NFLXX', 'GOOGX', 'HOODX', 'ORCLX', 'PLTRX', 'SPYX',
    'QQQX', 'TQQQX', 'CRCLX', 'DFDVX',
}

# Индексы — те что мы реально видели + индексы с цифрами ловит паттерн ниже.
# НЕ включаем VIX (может быть мем), GAS (Neo Gas крипто), CL/SI/PL/PA (двухбуквенные опасно).
KNOWN_INDICES = {
    'GER40', 'DAX40', 'US30', 'US100', 'US500', 'SPX500', 'NDX100',
    'HK50', 'HSI', 'JP225', 'NIK225', 'UK100', 'FR40', 'EU50', 'STOXX50',
    'AUS200', 'CHN50', 'IND50', 'CAN60', 'BR50', 'KOR200',
    'GVZ', 'OVX', 'BVZ', 'VXN', 'RVX', 'HSCHKD',
}

# Товары — НЕ ВКЛЮЧАЕМ короткие тикеры (NG, CL, HG, GC, SI, PL, PA, GAS — все могут быть крипто).
# Полагаемся на API contract_type для commodities.
KNOWN_COMMODITIES = {
    'WTI', 'BRENT',
}

# Металлы — X-префиксы (XAU, XAG и т.д.) и явные металлические токены.
KNOWN_METALS = {
    'XAU', 'XAG', 'XPT', 'XPD', 'XCU', 'XAL', 'XPB', 'XNI',
    'XAUT', 'PAXG', 'IAU', 'SLVON',
}

# Объединённый блэклист известных не-крипто тикеров
KNOWN_NON_CRYPTO = (
    KNOWN_STOCK_TICKERS | KNOWN_INDICES | KNOWN_COMMODITIES | KNOWN_METALS
)

# Регэксп для X-суффикса (TSLAX, MSFTX, NVDAX, COINX, MSTRX, METAX, GOOGLX, AAPLX)
# Минимум 3 буквы + X в конце. Но НЕ перед известными крипто-токенами.
_X_SUFFIX_RE = re.compile(r'^[A-Z]{3,8}X$')


def _is_filtered_symbol(symbol: str, contract_data: dict | None = None) -> str | None:
    """
    Проверить, нужно ли отфильтровать символ.

    Многослойная защита:
    1. contract_type из API (stocks, indices, metals, commodities, forex)
    2. Хардкод-список стейблкоинов
    3. Известные тикеры акций/индексов/металлов/товаров (когда Gate.io не проставил contract_type)
    4. Эвристики по паттернам имён (X-суффикс, цифры в конце)

    Args:
        symbol: Имя контракта (например, "TSM_USDT")
        contract_data: Данные контракта из API (содержит поле contract_type)

    Returns:
        Причина фильтрации или None если символ допустим.
    """
    # Извлекаем base currency: "USDC_USDT" -> "USDC"
    base = symbol.split('_')[0].upper() if '_' in symbol else symbol.upper()

    # СЛОЙ 1: contract_type из API
    if contract_data:
        contract_type = contract_data.get('contract_type', '')
        if contract_type in NON_CRYPTO_CONTRACT_TYPES:
            return f'{contract_type} ({base}) [API]'

        # СЛОЙ 1b: is_pre_market — pre-market доступно только для акций
        if contract_data.get('is_pre_market') is True:
            return f'pre_market_stock ({base}) [API]'

    # СЛОЙ 2: Стейблкоины
    if base in STABLECOINS:
        return f'stablecoin ({base})'

    # СЛОЙ 3: Известные тикеры акций/индексов/металлов/товаров
    if base in KNOWN_NON_CRYPTO:
        return f'known_non_crypto ({base}) [blacklist]'

    # СЛОЙ 4a: X-суффикс паттерн (TSLAX, MSFTX, NVDAX, COINX, AAPLX, METAX, GOOGLX)
    # Если базовое имя без X тоже известная акция — точно акция
    if _X_SUFFIX_RE.match(base) and len(base) >= 4:
        base_without_x = base[:-1]
        if base_without_x in KNOWN_NON_CRYPTO:
            return f'stock_x_variant ({base} -> {base_without_x}) [pattern]'

    # СЛОЙ 4b: Индексы с цифрами в конце (GER40, US30, HK50, JP225)
    # Паттерн: 2-5 букв + 2-3 цифры
    if re.match(r'^[A-Z]{2,5}\d{2,3}$', base):
        return f'index_pattern ({base}) [pattern]'

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
        self._retry_counts: Dict[str, int] = {}  # symbol -> количество неудачных попыток
        self._max_retries: int = 10  # Макс попыток перед permanent failure
        self._pending_type_check: Dict[str, float] = {}  # symbol -> timestamp когда перепроверить contract_type
        self._last_days_limit: Optional[int] = None  # Отслеживаем изменение настройки

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
        self._last_days_limit = self._get_listing_days_limit()

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
        self._retry_counts.pop(symbol, None)

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

        # Считаем количество неудачных попыток
        self._retry_counts[symbol] = self._retry_counts.get(symbol, 0) + 1
        retry_count = self._retry_counts[symbol]

        if permanent or retry_count >= self._max_retries:
            self._known_symbols.add(symbol)
            self._retry_after.pop(symbol, None)
            self._retry_counts.pop(symbol, None)
            if retry_count >= self._max_retries:
                logger.warning(f"Контракт {symbol}: исчерпаны попытки ({retry_count}/{self._max_retries}), больше не повторяем")
            else:
                logger.info(f"Контракт {symbol} помечен как окончательно неудачный, повтор не будет")
        else:
            self._retry_after[symbol] = time.time() + retry_minutes * 60
            logger.info(f"Контракт {symbol}: попытка {retry_count}/{self._max_retries}, повтор через {retry_minutes} мин")

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
            # Проверяем, не изменился ли days_since_listing_limit
            current_days_limit = self._get_listing_days_limit()
            if self._last_days_limit is not None and current_days_limit != self._last_days_limit:
                logger.info(f"🔄 Лимит дней изменён: {self._last_days_limit} → {current_days_limit}, сброс кеша контрактов")
                await self._load_known_symbols()
            self._last_days_limit = current_days_limit

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

                # Фильтруем не-крипто контракты (акции, индексы и т.д.) и стейблкоины
                filter_reason = _is_filtered_symbol(symbol, contract_data)
                if filter_reason:
                    logger.info(f"Контракт {symbol} отфильтрован: {filter_reason}")
                    self._known_symbols.add(symbol)
                    continue

                # ЗАЩИТА: если contract_type пустой — отложить решение на 5 мин,
                # дать Gate.io время проставить тип (иначе акции с пустым типом проскакивают)
                contract_type = contract_data.get('contract_type', '')
                if contract_type == '' and symbol not in self._pending_type_check:
                    # Узнаём возраст контракта
                    create_time = contract_data.get('create_time', 0)
                    age_seconds = time.time() - int(create_time) if create_time else 0
                    # Только для свежих контрактов (< 30 минут) — откладываем
                    if age_seconds < 1800:
                        self._pending_type_check[symbol] = time.time() + 300  # +5 минут
                        logger.info(
                            f"⏳ Контракт {symbol}: contract_type пустой, "
                            f"возраст {int(age_seconds/60)} мин — отложено на 5 мин для перепроверки"
                        )
                        continue

                # Если уже в pending — проверяем не пора ли
                if symbol in self._pending_type_check:
                    if time.time() < self._pending_type_check[symbol]:
                        continue  # Ещё ждём
                    # Время вышло — проверяем заново через индивидуальный API
                    del self._pending_type_check[symbol]
                    fresh_info = await self.api_client.get_contract_info(symbol)
                    if fresh_info:
                        fresh_filter = _is_filtered_symbol(symbol, fresh_info)
                        if fresh_filter:
                            logger.info(f"Контракт {symbol} отфильтрован после задержки: {fresh_filter}")
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

                # Уведомляем о новых листингах ПОСЛЕДОВАТЕЛЬНО
                # (create_task вызывал race condition — все проверяли лимит одновременно)
                for contract_info in new_contracts:
                    if contract_info['symbol'] not in symbols_to_process:
                        continue
                    for callback in self._on_new_listing_callbacks:
                        try:
                            await callback(contract_info['symbol'], contract_info['data'])
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
