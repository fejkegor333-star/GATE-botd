"""
WebSocket клиент для анализа стакана Gate.io
Подключается к futures WebSocket и анализирует order book
"""
import asyncio
import json
import logging
from typing import Optional, Dict, List, Callable, Any
from collections import deque
from datetime import datetime

import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError
from websockets.protocol import State as WebSocketState

from src.utils.config import config

logger = logging.getLogger(__name__)


class OrderBookEntry:
    """Элемент стакана"""

    def __init__(self, price: float, size: float):
        self.price = price
        self.size = size

    def __repr__(self):
        return f"OrderBookEntry(price={self.price}, size={self.size})"


class OrderBook:
    """Стакан ордеров"""

    def __init__(self, symbol: str, max_depth: int = 20):
        self.symbol = symbol
        self.max_depth = max_depth
        self.bids: List[OrderBookEntry] = []  # Покупки (asc)
        self.asks: List[OrderBookEntry] = []  # Продажи (asc)
        self.last_update: Optional[datetime] = None
        self.last_sequence = None
        self.subscribed_at: datetime = datetime.utcnow()  # Время подписки для таймаута

    def update(self, data: dict):
        """
        Обновить стакан из WebSocket сообщения

        Формат данных Gate.io Futures WebSocket:
        {
            "channel": "futures.order_book",
            "result": {
                "c": "BTC_USDT",
                "bids": [["price", "size"], ...],
                "asks": [["price", "size"], ...],
                "u": 12345,
                "t": 1234567890
            }
        }

        Args:
            data: Данные от WebSocket (могут быть в result или напрямую)
        """
        try:
            # Gate.io передает данные в result, но может быть и напрямую
            if 'result' in data:
                data = data['result']

            # Обновляем bids (покупки)
            # Gate.io может возвращать как dict {"p": price, "s": size}, так и array [price, size]
            bids_data = data.get('bids') or data.get('b')
            if bids_data:
                new_bids = []
                for bid in bids_data:
                    if isinstance(bid, dict):
                        price = float(bid.get('p', 0))
                        size = float(bid.get('s', 0))
                    else:
                        price = float(bid[0])
                        size = float(bid[1])
                    if size > 0:  # Только с объемом
                        new_bids.append(OrderBookEntry(price, size))

                # Сортируем по убыванию цены и берем топ N
                new_bids.sort(key=lambda x: x.price, reverse=True)
                self.bids = new_bids[:self.max_depth]

            # Обновляем asks (продажи)
            asks_data = data.get('asks') or data.get('a')
            if asks_data:
                new_asks = []
                for ask in asks_data:
                    if isinstance(ask, dict):
                        price = float(ask.get('p', 0))
                        size = float(ask.get('s', 0))
                    else:
                        price = float(ask[0])
                        size = float(ask[1])
                    if size > 0:  # Только с объемом
                        new_asks.append(OrderBookEntry(price, size))

                # Сортируем по возрастанию цены и берем топ N
                new_asks.sort(key=lambda x: x.price)
                self.asks = new_asks[:self.max_depth]

            self.last_update = datetime.utcnow()

            # Sequence number для проверки пропусков
            if 'u' in data:
                self.last_sequence = data['u']

        except Exception as e:
            logger.error(f"Ошибка обновления стакана: {e}")

    def get_best_bid(self) -> Optional[float]:
        """Лучшая цена покупки"""
        return self.bids[0].price if self.bids else None

    def get_best_ask(self) -> Optional[float]:
        """Лучшая цена продажи"""
        return self.asks[0].price if self.asks else None

    def get_spread(self) -> Optional[float]:
        """Спред между лучшими ценами"""
        best_bid = self.get_best_bid()
        best_ask = self.get_best_ask()
        if best_bid and best_ask:
            return best_ask - best_bid
        return None

    def get_spread_pct(self) -> Optional[float]:
        """Спред в процентах"""
        best_bid = self.get_best_bid()
        best_ask = self.get_best_ask()
        if best_bid and best_ask:
            return ((best_ask - best_bid) / best_bid) * 100
        return None

    def get_total_bid_volume(self) -> float:
        """Общий объем на покупку в стакане"""
        return sum(entry.size for entry in self.bids)

    def get_total_ask_volume(self) -> float:
        """Общий объем на продажу в стакане"""
        return sum(entry.size for entry in self.asks)

    def get_volume_imbalance(self) -> float:
        """
        Дисбаланс объема: -1 (все продажи) до +1 (все покупки)

        Returns:
            Значение от -1 до +1
        """
        bid_vol = self.get_total_bid_volume()
        ask_vol = self.get_total_ask_volume()
        total = bid_vol + ask_vol

        if total == 0:
            return 0.0

        return (bid_vol - ask_vol) / total

    def get_vwap(self, side: str, depth: int = 5) -> Optional[float]:
        """
        VWAP (Volume Weighted Average Price) для стороны

        Args:
            side: 'bid' или 'ask'
            depth: Глубина для расчета

        Returns:
            VWAP цена или None
        """
        entries = self.bids if side == 'bid' else self.asks
        if not entries:
            return None

        total_volume = 0.0
        total_value = 0.0

        for entry in entries[:depth]:
            total_volume += entry.size
            total_value += entry.price * entry.size

        if total_volume == 0:
            return None

        return total_value / total_volume

    def should_buy_signal(self) -> bool:
        """
        Проверить условия для сигнала на покупку (LONG)

        Условия:
        1. Дисбаланс объема > порога (много покупок)
        2. Объем на покупку > минимального порога

        Returns:
            True если условия выполнены
        """
        imbalance = self.get_volume_imbalance()
        bid_volume = self.get_total_bid_volume()

        # Проверяем дисбаланс
        if imbalance < config.websocket.volume_imbalance_threshold:
            return False

        # Проверяем объем
        # Примерная оценка объема в USDT: цена * объем
        best_bid = self.get_best_bid()
        if not best_bid:
            return False

        estimated_volume_usdt = best_bid * bid_volume

        if estimated_volume_usdt < config.websocket.min_volume_threshold_usdt:
            return False

        logger.info(
            f"Сигнал на покупку {self.symbol}: "
            f"imbalance={imbalance:.2f}, "
            f"volume=${estimated_volume_usdt:.0f}"
        )

        return True

    def should_sell_signal(self, min_liquidity_usdt: Optional[float] = None) -> bool:
        """
        Проверить условия для сигнала на продажу (SHORT)

        Для SHORT стратегии на новых листингах:
        - Стакан заполнен маркетмейкером (много ликвидности)
        - Минимум 5 уровней с обеих сторон
        - Спред <= 1%
        - Объем на лучшем уровне >= порога (min_liquidity_usdt из настроек БД)

        Args:
            min_liquidity_usdt: Минимальная ликвидность из настроек БД (ТЗ п. 4.2.3)

        Returns:
            True если условия выполнены
        """
        # Используем переданное значение или значение из конфига как fallback
        min_liq = min_liquidity_usdt if min_liquidity_usdt is not None else config.websocket.min_volume_threshold_usdt

        # Проверяем количество уровней
        if len(self.bids) < config.websocket.min_order_book_levels:
            return False
        if len(self.asks) < config.websocket.min_order_book_levels:
            return False

        # Проверяем спред
        spread_pct = self.get_spread_pct()
        if spread_pct is None or spread_pct > config.websocket.max_spread_percentage:
            return False

        # Проверяем объем на лучшем уровне bid (для SHORT это важно)
        best_bid = self.get_best_bid()
        if not best_bid:
            return False

        # Объем на лучшем уровне bid
        best_bid_volume = self.bids[0].size if self.bids else 0
        estimated_volume_usdt = best_bid * best_bid_volume

        # Проверяем минимальную ликвидность из настроек БД (согласно ТЗ)
        if estimated_volume_usdt < min_liq:
            return False

        # Проверяем общий объем стакана
        total_volume = self.get_total_bid_volume() + self.get_total_ask_volume()
        total_volume_usdt = best_bid * total_volume

        if total_volume_usdt < min_liq * 10:
            return False

        logger.info(
            f"🟢 Сигнал на SHORT {self.symbol}: "
            f"spread={spread_pct:.2f}%, "
            f"bid_volume=${estimated_volume_usdt:.0f}, "
            f"total_volume=${total_volume_usdt:.0f}"
        )

        return True


class GateWebSocketClient:
    """WebSocket клиент для Gate.io Futures"""

    def __init__(self):
        self.ws = None  # websockets.ClientConnection (v16+)
        self.order_books: Dict[str, OrderBook] = {}
        self._running = False
        self._reconnect_count = 0
        self._callbacks: List[Callable] = []

    def on_order_book_update(self, callback: Callable[[str, OrderBook], None]):
        """Добавить callback на обновление стакана"""
        self._callbacks.append(callback)

    async def connect(self):
        """Подключиться к WebSocket"""
        url = config.gate.ws_url
        logger.info(f"Подключение к WebSocket: {url}")

        try:
            self.ws = await websockets.connect(
                url,
                ping_interval=config.websocket.ping_interval_seconds,
                close_timeout=10,
            )
            self._reconnect_count = 0
            logger.info("WebSocket подключен")

        except Exception as e:
            logger.error(f"Ошибка подключения к WebSocket: {e}")
            raise

    async def disconnect(self):
        """Отключиться от WebSocket"""
        self._running = False

        if self.ws:
            await self.ws.close()
            self.ws = None
            logger.info("WebSocket отключен")

    async def subscribe_order_book(self, symbol: str, depth: int = None):
        """
        Подписаться на стакан контракта

        Формат подписки по документации Gate.io Futures WebSocket v4:
        {
            "time": 1234567890,
            "channel": "futures.order_book",
            "event": "subscribe",
            "payload": ["BTC_USDT"]
        }

        Args:
            symbol: Символ контракта (например, BTC_USDT)
            depth: Глубина стакана (по умолчанию из конфига)
        """
        if depth is None:
            depth = config.websocket.order_book_depth

        # Создаем стакан
        self.order_books[symbol] = OrderBook(symbol, depth)

        # Формируем запрос подписки по формату Gate.io
        # payload: [contract, depth, frequency] — "0" = real-time updates
        import time
        payload = {
            "time": int(time.time()),
            "channel": "futures.order_book",
            "event": "subscribe",
            "payload": [symbol, str(depth), "0"]
        }

        try:
            if self.is_connected():
                await self.ws.send(json.dumps(payload))
                logger.info(f"Подписка на стакан {symbol} (глубина: {depth})")
            else:
                logger.warning("WebSocket не подключен")

        except Exception as e:
            logger.error(f"Ошибка подписки на стакан {symbol}: {e}")

    async def unsubscribe_order_book(self, symbol: str):
        """Отписаться от стакана"""
        if symbol in self.order_books:
            del self.order_books[symbol]

        import time as _time
        payload = {
            "time": int(_time.time()),
            "channel": "futures.order_book",
            "event": "unsubscribe",
            "payload": [symbol],
        }

        try:
            if self.is_connected():
                await self.ws.send(json.dumps(payload))
                logger.info(f"Отписка от стакана {symbol}")

        except Exception as e:
            logger.error(f"Ошибка отписки от стакана {symbol}: {e}")

    async def listen(self):
        """Слушать сообщения от WebSocket"""
        if not self.ws:
            raise RuntimeError("WebSocket не подключен")

        self._running = True
        logger.info("Начало прослушивания WebSocket")

        try:
            async for message in self.ws:
                if not self._running:
                    break

                try:
                    data = json.loads(message)
                    await self._handle_message(data)

                except json.JSONDecodeError as e:
                    logger.error(f"Ошибка парсинга JSON: {e}")
                except Exception as e:
                    logger.error(f"Ошибка обработки сообщения: {e}")

        except ConnectionClosed:
            logger.warning("Соединение WebSocket закрыто")
        except Exception as e:
            logger.error(f"Ошибка в цикле прослушивания: {e}")
        finally:
            self._running = False

    async def _handle_message(self, data: dict):
        """Обработать сообщение от WebSocket"""
        try:
            # Проверяем тип сообщения
            if 'channel' in data:
                channel = data['channel']

                # Обновление стакана
                if channel == 'futures.order_book':
                    await self._handle_order_book_update(data['result'])

            elif 'event' in data:
                event = data['event']
                logger.debug(f"Событие WebSocket: {event}")

        except Exception as e:
            logger.error(f"Ошибка обработки сообщения: {e}")

    async def _handle_order_book_update(self, data: dict):
        """Обработать обновление стакана

        Формат данных от Gate.io Futures WebSocket:
        {
            "channel": "futures.order_book",
            "result": {
                "c": "BTC_USDT",  // контракт
                "bids": [["price", "size"], ...],
                "asks": [["price", "size"], ...],
                "u": 12345,       // sequence number
                "t": 1234567890   // timestamp
            }
        }
        """
        try:
            # Извлекаем символ из данных (Gate.io использует 'c' для контракта)
            symbol = data.get('c', '') or data.get('contract', '') or data.get('s', '')
            if not symbol:
                logger.debug(f"Нет символа в данных стакана: {data.keys()}")
                return

            # Обновляем стакан
            if symbol in self.order_books:
                self.order_books[symbol].update(data)

                # Проверяем сигналы
                order_book = self.order_books[symbol]

                # Вызываем callbacks
                for callback in self._callbacks:
                    try:
                        await callback(symbol, order_book)
                    except Exception as e:
                        logger.error(f"Ошибка в callback: {e}")

        except Exception as e:
            logger.error(f"Ошибка обновления стакана: {e}")

    async def reconnect(self):
        """Переподключиться к WebSocket"""
        await self.disconnect()

        if self._reconnect_count >= config.websocket.max_reconnect_attempts:
            logger.error("Превышено максимальное количество попыток переподключения")
            return False

        self._reconnect_count += 1
        logger.info(f"Попытка переподключения {self._reconnect_count}/{config.websocket.max_reconnect_attempts}")

        await asyncio.sleep(config.websocket.reconnect_interval_seconds)

        try:
            await self.connect()

            # Переподписываемся на все стаканы
            for symbol in list(self.order_books.keys()):
                await self.subscribe_order_book(symbol)

            return True

        except Exception as e:
            logger.error(f"Ошибка переподключения: {e}")
            return False

    def get_order_book(self, symbol: str) -> Optional[OrderBook]:
        """Получить стакан для символа"""
        return self.order_books.get(symbol)

    def is_connected(self) -> bool:
        """Проверить подключение"""
        if self.ws is None:
            return False
        try:
            return self.ws.state == WebSocketState.OPEN
        except Exception:
            return False


# Глобальный инстанс
ws_client = GateWebSocketClient()
