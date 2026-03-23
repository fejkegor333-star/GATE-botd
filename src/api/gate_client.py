"""
Клиент для работы с Gate.io API
"""
import logging
import asyncio
import aiohttp
import hmac
import hashlib
import time
from typing import Optional, Dict, Any, List
from datetime import datetime

from src.utils.config import config
from src.db.connection import db
from src.db.models import Contract

logger = logging.getLogger(__name__)

# Префикс API v4
API_V4_PREFIX = '/api/v4'


class GateApiClient:
    """Асинхронный клиент для Gate.io API"""

    def __init__(self):
        # HTTP клиент для асинхронных запросов
        self._session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        """Получить или создать HTTP сессию"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=config.gate.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        """Закрыть HTTP сессию"""
        if self._session and not self._session.closed:
            await self._session.close()

    def _build_url(self, path: str) -> str:
        """
        Построить полный URL для API запроса

        Args:
            path: Путь без /api/v4 (например, /futures/usdt/contracts)

        Returns:
            Полный URL (например, https://api.gateio.ws/api/v4/futures/usdt/contracts)
        """
        return f"{config.gate.api_url}{API_V4_PREFIX}{path}"

    def _get_headers(self) -> Dict[str, str]:
        """Получить заголовки для публичного запроса"""
        return {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def _get_auth_headers(self, method: str, url_path: str, query_string: str = '', body: str = '') -> Dict[str, str]:
        """
        Генерировать заголовки аутентификации для Gate.io API

        Формат подписи:
        method\n/api/v4/path\nquery_string\nbody_sha512_hash\ntimestamp

        Args:
            method: HTTP метод (GET, POST, и т.д.)
            url_path: Путь URL без /api/v4 (например, /futures/usdt/accounts)
            query_string: Строка запроса (если есть)
            body: Тело запроса (для POST/PUT)

        Returns:
            Заголовки для авторизованного запроса
        """
        timestamp = str(int(time.time()))
        method_upper = method.upper()

        # Полный путь для подписи всегда с /api/v4 префиксом
        signature_path = f'{API_V4_PREFIX}{url_path}'

        # Хешируем тело (SHA512)
        body_for_hash = body if body else ''
        body_hash = hashlib.sha512(body_for_hash.encode('utf-8')).hexdigest()

        # Формируем payload: method\npath\nquery_string\nbody_hash\ntimestamp
        payload = f"{method_upper}\n{signature_path}\n{query_string}\n{body_hash}\n{timestamp}"

        # Генерируем HMAC-SHA512
        signature = hmac.new(
            config.gate.api_secret.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()

        return {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'KEY': config.gate.api_key,
            'Timestamp': timestamp,
            'SIGN': signature,
        }

    async def fetch_contracts(self, max_retries: int = 3) -> List[Dict[str, Any]]:
        """
        Получить список всех фьючерсных контрактов USDT

        Args:
            max_retries: Максимальное количество попыток при ошибке

        Returns:
            Список контрактов
        """
        # Таймаут для мониторинга: быстрый connect, но достаточно времени на скачивание большого JSON
        short_timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=45)

        for attempt in range(max_retries):
            try:
                session = await self.get_session()
                url_path = config.monitoring.contracts_endpoint
                url = self._build_url(url_path)

                async with session.get(url, headers=self._get_headers(), timeout=short_timeout) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"API Error {response.status}: {error_text[:200]}")
                        raise aiohttp.ClientResponseError(
                            request_info=response.request_info,
                            history=response.history,
                            status=response.status,
                            message=error_text[:200],
                        )

                    data = await response.json()
                    logger.debug(f"Получено {len(data)} контрактов")
                    return data

            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 5  # 5, 10, 15 сек
                    logger.warning(f"Timeout/Error при получении контрактов (попытка {attempt + 1}/{max_retries}). "
                                   f"Повтор через {wait_time} сек... Ошибка: {e}")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Не удалось получить контракты после {max_retries} попыток: {e}")
                    raise
            except Exception as e:
                logger.error(f"Неожиданная ошибка при получении контрактов: {e}")
                raise

    async def fetch_candles(
        self,
        contract: str,
        interval: str = '1w',
        limit: int = 1000,
        from_: Optional[int] = None,
        to: Optional[int] = None
    ) -> List[List[Any]]:
        """
        Получить свечи (OHLCV)

        Args:
            contract: Символ контракта (например, BTC_USDT)
            interval: Интервал свечи (1m, 5m, 1h, 1d, 1w)
            limit: Количество свечей (макс 1000)
            from_: Начальная временная метка (секунды)
            to: Конечная временная метка (секунды)

        Returns:
            Список свечей: [[timestamp, open, high, low, close, volume], ...]
        """
        try:
            session = await self.get_session()
            url_path = config.ath.candles_endpoint
            url = self._build_url(url_path)

            params = {
                'contract': contract,
                'interval': interval,
                'limit': limit,
            }

            if from_:
                params['from'] = from_
            if to:
                params['to'] = to

            async with session.get(url, params=params, headers=self._get_headers()) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"API Error {response.status}: {error_text[:200]}")
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=error_text[:200],
                    )

                data = await response.json()
                logger.debug(f"Получено {len(data)} свечей для {contract}")
                return data

        except aiohttp.ClientResponseError as e:
            logger.error(f"Ошибка API при получении свечей для {contract}: {e}")
            raise
        except Exception as e:
            logger.error(f"Неожиданная ошибка при получении свечей: {e}")
            raise

    async def get_position(self, contract: str) -> Dict[str, Any]:
        """
        Получить информацию о позиции

        Args:
            contract: Символ контракта

        Returns:
            Информация о позиции
        """
        try:
            session = await self.get_session()
            url_path = '/futures/usdt/positions'
            url = self._build_url(url_path)

            params = {'contract': contract}
            query_string = f'contract={contract}'

            # Используем авторизованные заголовки
            headers = self._get_auth_headers('GET', url_path, query_string=query_string)

            async with session.get(url, params=params, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"API Error {response.status}: {error_text[:200]}")
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=error_text[:200],
                    )

                data = await response.json()

                # API возвращает массив, берем первую позицию
                if data and len(data) > 0:
                    return data[0]

                return {}

        except aiohttp.ClientResponseError as e:
            logger.error(f"Ошибка API при получении позиции {contract}: {e}")
            raise
        except Exception as e:
            logger.error(f"Неожиданная ошибка при получении позиции: {e}")
            raise

    async def get_all_positions(self) -> List[Dict[str, Any]]:
        """
        Получить все открытые позиции с биржи

        Returns:
            Список позиций с ненулевым size
        """
        try:
            session = await self.get_session()
            url_path = '/futures/usdt/positions'
            url = self._build_url(url_path)

            headers = self._get_auth_headers('GET', url_path)

            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"API Error {response.status}: {error_text[:200]}")
                    return []

                data = await response.json()

                if not data:
                    return []

                # Фильтруем только позиции с ненулевым size
                return [pos for pos in data if int(pos.get('size', 0) or 0) != 0]

        except Exception as e:
            logger.error(f"Ошибка получения всех позиций: {e}")
            return []

    async def get_futures_balance(self) -> Dict[str, Any]:
        """
        Получить баланс фьючерсного счета

        Returns:
            Информация о балансе
        """
        try:
            session = await self.get_session()
            url_path = '/futures/usdt/accounts'
            url = self._build_url(url_path)

            # Используем заголовки аутентификации
            headers = self._get_auth_headers('GET', url_path)

            async with session.get(url, headers=headers) as response:
                if response is None or not hasattr(response, 'status'):
                    logger.debug("API вернул None для баланса фьючерсов")
                    return {}

                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"API Error {response.status}: {error_text[:200]}")
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=error_text[:200],
                    )

                data = await response.json()
                return data

        except aiohttp.ClientResponseError as e:
            logger.debug(f"Ошибка API при получении баланса фьючерсов: {e}")
            return {}
        except Exception as e:
            logger.debug(f"Неожиданная ошибка при получении баланса: {e}")
            return {}

    async def get_spot_balance(self) -> Dict[str, Any]:
        """
        Получить баланс спотового счета

        Returns:
            Информация о балансе
        """
        try:
            session = await self.get_session()
            url_path = '/spot/accounts'
            url = self._build_url(url_path)

            # Используем заголовки аутентификации
            headers = self._get_auth_headers('GET', url_path)

            async with session.get(url, headers=headers) as response:
                if response is None or not hasattr(response, 'status'):
                    logger.debug("API вернул None для баланса спота")
                    return {}

                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"API Error {response.status}: {error_text[:200]}")
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=error_text[:200],
                    )

                data = await response.json()
                return data

        except aiohttp.ClientResponseError as e:
            logger.debug(f"Ошибка API при получении баланса спота: {e}")
            return {}
        except Exception as e:
            logger.debug(f"Неожиданная ошибка при получении баланса: {e}")
            return {}

    async def get_ath_price(self, contract: str) -> Optional[float]:
        """
        Получить ATH (All-Time High) цену по недельным свечам

        Args:
            contract: Символ контракта (например, BTC_USDT)

        Returns:
            ATH цена или None
        """
        try:
            # Получаем недельные свечи за все время
            candles = await self.fetch_candles(
                contract=contract,
                interval='1w',
                limit=config.ath.lookback_weeks,
            )

            if not candles:
                logger.warning(f"Нет свечей для расчета ATH: {contract}")
                return None

            # Находим максимальную цену (high)
            # Gate.io может возвращать как dict {"h": high, ...}, так и array [ts, open, high, low, close, vol]
            def get_high(candle):
                if isinstance(candle, dict):
                    return float(candle.get('h', 0))
                return float(candle[2])

            ath = max(get_high(candle) for candle in candles)

            logger.debug(f"ATH для {contract}: ${ath:.6f}")
            return ath

        except Exception as e:
            logger.error(f"Ошибка получения ATH для {contract}: {e}")
            return None

    async def get_ticker(self, contract: str) -> Optional[Dict[str, Any]]:
        """
        Получить текущий тикер (цену) для контракта

        Args:
            contract: Символ контракта (например, BTC_USDT)

        Returns:
            Данные тикера или None
        """
        try:
            session = await self.get_session()
            url_path = f'/futures/usdt/tickers?contract={contract}'
            url = self._build_url(url_path)

            async with session.get(url, headers=self._get_headers()) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Ошибка получения тикера {contract}: {response.status} {error_text[:200]}")
                    return None

                data = await response.json()
                if data and isinstance(data, list) and len(data) > 0:
                    return data[0]
                return None

        except Exception as e:
            logger.error(f"Ошибка получения тикера {contract}: {e}")
            return None

    async def get_contract_info(self, contract: str) -> Optional[Dict[str, Any]]:
        """
        Получить полную информацию о контракте

        Args:
            contract: Символ контракта (например, BTC_USDT)

        Returns:
            Данные контракта или None
        """
        try:
            session = await self.get_session()
            url_path = f'/futures/usdt/contracts/{contract}'
            url = self._build_url(url_path)

            async with session.get(url, headers=self._get_headers()) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Ошибка получения контракта {contract}: {response.status} {error_text[:200]}")
                    return None

                return await response.json()

        except Exception as e:
            logger.error(f"Ошибка получения контракта {contract}: {e}")
            return None

    async def get_max_leverage(self, contract: str) -> int:
        """
        Получить максимальное доступное плечо для контракта

        Args:
            contract: Символ контракта (например, BTC_USDT)

        Returns:
            Максимальное плечо (leverage_max) или 20 по умолчанию
        """
        try:
            session = await self.get_session()
            url_path = f'/futures/usdt/contracts/{contract}'
            url = self._build_url(url_path)

            async with session.get(url, headers=self._get_headers()) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Ошибка получения контракта {contract}: {response.status} {error_text[:200]}")
                    return 20

                data = await response.json()
                max_lev = int(data.get('leverage_max', 20))
                logger.info(f"Max leverage для {contract}: x{max_lev}")
                return max_lev

        except Exception as e:
            logger.error(f"Ошибка получения max leverage {contract}: {e}")
            return 20

    async def set_leverage(self, contract: str, leverage: int = 20) -> int:
        """
        Установить кредитное плечо для контракта

        Args:
            contract: Символ контракта (например, BTC_USDT)
            leverage: Кредитное плечо (по умолчанию 20)

        Returns:
            Фактическое плечо, установленное Gate.io (0 при ошибке)
        """
        try:
            session = await self.get_session()
            url_path = f'/futures/usdt/positions/{contract}/leverage'
            url = self._build_url(url_path)

            query_string = f'leverage={leverage}'
            headers = self._get_auth_headers('POST', url_path, query_string=query_string)

            async with session.post(url, params={'leverage': leverage}, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Ошибка установки leverage {contract}: {response.status} {error_text[:200]}")
                    return 0

                data = await response.json()
                # Dual mode: Gate.io возвращает список [{long}, {short}]
                # Single mode: возвращает dict
                if isinstance(data, list):
                    # Берём первый элемент (оба имеют одинаковый leverage)
                    data = data[0] if data else {}
                # Gate.io возвращает фактическое плечо в ответе
                actual_leverage = int(data.get('leverage', 0) or 0)
                cross_limit = int(data.get('cross_leverage_limit', 0) or 0)
                # В cross-margin mode leverage=0 и работает cross_leverage_limit
                effective = actual_leverage if actual_leverage > 0 else cross_limit
                if effective != leverage:
                    logger.warning(
                        f"Leverage {contract}: запрошено x{leverage}, "
                        f"фактически x{effective} (leverage={actual_leverage}, cross_limit={cross_limit})"
                    )
                else:
                    logger.info(f"Leverage установлен: {contract} x{effective}")
                return effective if effective > 0 else leverage

        except Exception as e:
            logger.error(f"Ошибка установки leverage {contract}: {e}")
            return 0

    async def place_futures_order(
        self,
        contract: str,
        size: int,
        price: str = "0",
        tif: str = "ioc",
        close: bool = False,
        reduce_only: bool = False,
        auto_size: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Разместить ордер на фьючерсах Gate.io

        Args:
            contract: Символ контракта (например, BTC_USDT)
            size: Размер в контрактах (отрицательный = SHORT)
            price: Цена (0 = рыночный ордер)
            tif: Time in force (gtc, ioc, poc)
            close: True = закрыть позицию целиком (single mode)
            reduce_only: True = только уменьшать позицию (dual mode)
            auto_size: "close_short" или "close_long" (dual mode auto-close)

        Returns:
            Данные ордера или None
        """
        try:
            import json as _json
            session = await self.get_session()
            url_path = '/futures/usdt/orders'
            url = self._build_url(url_path)

            payload = {
                'contract': contract,
                'size': size,
                'price': price,
                'tif': tif,
            }
            if auto_size:
                # Dual mode: auto_size требует close=false, reduce_only=true
                payload['auto_size'] = auto_size
                payload['close'] = False
                payload['reduce_only'] = True
            else:
                if close:
                    payload['close'] = True
                if reduce_only:
                    payload['reduce_only'] = True

            body = _json.dumps(payload)
            headers = self._get_auth_headers('POST', url_path, body=body)

            async with session.post(url, json=payload, headers=headers) as response:
                if response.status not in (200, 201):
                    error_text = await response.text()
                    # Проверяем ошибку dual-mode для авто-переключения
                    if 'POSITION_DUAL_MODE' in error_text and not auto_size:
                        logger.warning(f"Dual mode обнаружен для {contract}, повторяем с auto_size")
                        return None
                    logger.error(f"Ошибка создания ордера {contract}: {response.status} {error_text[:200]}")
                    # Возвращаем ошибку с меткой, чтобы caller мог обработать
                    if 'INSUFFICIENT_AVAILABLE' in error_text:
                        return {'_error': 'INSUFFICIENT_AVAILABLE', '_raw': error_text[:300]}
                    return None

                data = await response.json()
                logger.info(f"Ордер создан: {contract} size={size} id={data.get('id')}")
                return data

        except Exception as e:
            logger.error(f"Ошибка размещения ордера {contract}: {e}")
            return None

    async def update_contract_ath(self, symbol: str) -> Optional[float]:
        """
        Обновить ATH в базе данных

        Args:
            symbol: Символ контракта

        Returns:
            ATH цена или None
        """
        try:
            ath = await self.get_ath_price(symbol)

            if ath:
                with db.get_session() as session:
                    contract = session.query(Contract).filter(
                        Contract.symbol == symbol
                    ).first()

                    if contract:
                        contract.ath_price = ath
                        contract.ath_updated_at = datetime.utcnow()
                        session.commit()
                        logger.info(f"Обновлен ATH для {symbol}: ${ath:.6f}")

            return ath

        except Exception as e:
            logger.error(f"Ошибка обновления ATH для {symbol}: {e}")
            return None
