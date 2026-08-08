"""Обнаружение позиций, закрытых вне бота.

Баг 08.08.2026: бот показывал две позиции (ZEST_USDT, UP_USDT), которых на
бирже не было. Причина: get_all_positions() возвращал [] и при сбое API, и
когда позиций реально нет. Защита в detect_externally_closed() считала любой
пустой ответ сбоем и НИКОГДА не чистила позиции — фантомы висели вечно.

Правило: None = не смогли прочитать биржу (не трогаем), [] = биржа ответила,
позиций нет (чистим).
"""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from src.api.gate_client import GateApiClient
from src.trading.trader import PositionManager


# ── клиент API: пустой ответ и сбой должны различаться ───────────────────────

class _FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, response=None, raises: Exception | None = None):
        self._response = response
        self._raises = raises

    def get(self, *args, **kwargs):
        if self._raises:
            raise self._raises
        return self._response


def _client_with(session) -> GateApiClient:
    client = GateApiClient()
    client.get_session = Mock(return_value=_awaitable(session))
    return client


def _awaitable(value):
    async def _coro():
        return value
    return _coro()


@pytest.mark.asyncio
async def test_empty_exchange_returns_empty_list():
    """Биржа ответила 200 и пустым списком — позиций реально нет."""
    client = _client_with(_FakeSession(_FakeResponse(200, [])))
    assert await client.get_all_positions() == []


@pytest.mark.asyncio
async def test_http_error_returns_none():
    """Сбой API — None, чтобы вызывающий код не принял это за «позиций нет»."""
    client = _client_with(_FakeSession(_FakeResponse(502, 'bad gateway')))
    assert await client.get_all_positions() is None


@pytest.mark.asyncio
async def test_exception_returns_none():
    client = _client_with(_FakeSession(raises=RuntimeError('нет сети')))
    assert await client.get_all_positions() is None


@pytest.mark.asyncio
async def test_zero_size_positions_are_filtered():
    """Gate отдаёт закрытые контракты с size=0 — это не позиции."""
    payload = [{'contract': 'A_USDT', 'size': 0}, {'contract': 'B_USDT', 'size': -5}]
    client = _client_with(_FakeSession(_FakeResponse(200, payload)))
    result = await client.get_all_positions()
    assert [p['contract'] for p in result] == ['B_USDT']


# ── менеджер позиций: чистим фантомы, но не при сбое ─────────────────────────

@pytest.fixture
def manager():
    m = PositionManager()
    m._active_positions = {}
    return m


def _fake_position(symbol: str):
    pos = Mock()
    pos.contract_symbol = symbol
    pos.entry_price = 0.122
    pos.current_price = 0.176
    pos.total_volume_usdt = 10.0
    return pos


def _with_positions(manager, *symbols):
    for s in symbols:
        manager._active_positions[s] = _fake_position(s)


def _mock_db():
    """БД-заглушка: сессия отдаёт Mock на любой запрос."""
    mock_db = patch('src.trading.trader.db').start()
    session = Mock()
    session.query.return_value.filter.return_value.first.return_value = Mock()
    mock_db.get_session.return_value.__enter__.return_value = session
    return mock_db


@pytest.mark.asyncio
async def test_phantom_positions_are_closed_when_exchange_is_empty(manager):
    """Главный баг: биржа пуста, у нас 2 позиции — обе должны закрыться."""
    _with_positions(manager, 'ZEST_USDT', 'UP_USDT')

    with patch('src.trading.trader.db') as mock_db, \
         patch('src.trading.trader.config') as mock_config, \
         patch.object(manager.api_client, 'get_all_positions', return_value=[]):
        mock_config.dry_run = False
        mock_db.get_session.return_value.__enter__.return_value = Mock()

        await manager.detect_externally_closed()          # 1-я проверка
        closed = await manager.detect_externally_closed()  # подтверждение

    assert sorted(closed) == ['UP_USDT', 'ZEST_USDT']
    assert manager._active_positions == {}


@pytest.mark.asyncio
async def test_api_failure_keeps_positions(manager):
    """None = биржу прочитать не удалось. Позиции не трогаем."""
    _with_positions(manager, 'ZEST_USDT')

    with patch('src.trading.trader.db'), \
         patch('src.trading.trader.config') as mock_config, \
         patch.object(manager.api_client, 'get_all_positions', return_value=None):
        mock_config.dry_run = False

        assert await manager.detect_externally_closed() == []
        assert await manager.detect_externally_closed() == []

    assert 'ZEST_USDT' in manager._active_positions


@pytest.mark.asyncio
async def test_single_miss_does_not_close(manager):
    """Одного пропуска мало: лимитка могла ещё не залиться."""
    _with_positions(manager, 'ZEST_USDT')

    with patch('src.trading.trader.db'), \
         patch('src.trading.trader.config') as mock_config, \
         patch.object(manager.api_client, 'get_all_positions', return_value=[]):
        mock_config.dry_run = False
        assert await manager.detect_externally_closed() == []

    assert 'ZEST_USDT' in manager._active_positions


@pytest.mark.asyncio
async def test_position_reappearing_resets_streak(manager):
    """Позиция пропала на один тик и вернулась — не закрываем."""
    _with_positions(manager, 'ZEST_USDT')
    present = [{'contract': 'ZEST_USDT', 'size': -100}]

    with patch('src.trading.trader.db'), \
         patch('src.trading.trader.config') as mock_config, \
         patch.object(manager.api_client, 'get_all_positions') as api:
        mock_config.dry_run = False

        api.return_value = []
        await manager.detect_externally_closed()
        api.return_value = present
        await manager.detect_externally_closed()   # вернулась — счётчик сброшен
        api.return_value = []
        assert await manager.detect_externally_closed() == []

    assert 'ZEST_USDT' in manager._active_positions


@pytest.mark.asyncio
async def test_dry_run_positions_are_never_touched(manager):
    """В DRY_RUN позиций на бирже нет по определению — это не «закрыты вручную»."""
    _with_positions(manager, 'ZEST_USDT')

    with patch('src.trading.trader.db'), \
         patch('src.trading.trader.config') as mock_config, \
         patch.object(manager.api_client, 'get_all_positions', return_value=[]) as api:
        mock_config.dry_run = True

        assert await manager.detect_externally_closed() == []
        assert await manager.detect_externally_closed() == []
        api.assert_not_called()

    assert 'ZEST_USDT' in manager._active_positions


@pytest.mark.asyncio
async def test_sync_from_exchange_ignores_api_failure(manager):
    """При сбое API восстановление позиций не должно решать, что биржа пуста."""
    with patch('src.trading.trader.db'), \
         patch.object(manager.api_client, 'get_all_positions', return_value=None), \
         patch.object(manager, '_get_trading_settings') as settings:
        await manager._sync_positions_from_exchange()
        settings.assert_not_called()
