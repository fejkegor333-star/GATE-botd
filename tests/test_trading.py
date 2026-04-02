"""
Тесты торгового модуля SHORT
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from src.trading.trader import PositionManager


@pytest.fixture
def position_manager():
    """Создать менеджер позиций"""
    manager = PositionManager()
    manager._active_positions = {}
    return manager


class TestPositionManager:
    """Тесты менеджера позиций SHORT"""

    def test_creation(self, position_manager):
        """Тест создания менеджера"""
        assert position_manager is not None
        assert len(position_manager._active_positions) == 0

    def test_get_empty_position(self, position_manager):
        """Тест получения несуществующей позиции"""
        assert position_manager.get_position('BTC_USDT') is None

    def test_should_add_averaging_no_position(self, position_manager):
        """Тест усреднения без позиции"""
        result = position_manager.should_add_averaging('BTC_USDT', 100.0)
        assert result is None

    def test_should_close_position_no_position(self, position_manager):
        """Тест закрытия без позиции"""
        result = position_manager.should_close_position('BTC_USDT', 100.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_open_position(self, position_manager):
        """Тест открытие позиции SHORT"""
        with patch('src.trading.trader.db') as mock_db, \
             patch('src.trading.trader.config') as mock_config:
            mock_config.dry_run = True

            # Мокаем сессию
            mock_session = Mock()
            mock_session.add = Mock()
            mock_session.commit = Mock()
            mock_session.refresh = Mock()
            mock_session.expunge = Mock()

            mock_db.get_session.return_value.__enter__.return_value = mock_session

            # Мокаем настройки из БД
            with patch.object(position_manager, '_get_trading_settings', return_value={
                'max_concurrent_coins': 10,
                'max_avg_count': 3,
                'avg_levels': [300, 700, 1000],
                'take_profit_pct': 2.0,
                'ath_ratio_threshold': 0.3,
                'days_since_listing_limit': 30,
            }):
                # Мокаем blacklist проверку
                with patch.object(position_manager, '_is_blacklisted', return_value=False):
                    # Мокаем API для dry_run (get_contract_info)
                    with patch.object(position_manager.api_client, 'get_contract_info', return_value={
                        'quanto_multiplier': '1', 'leverage_max': '20',
                    }):
                        # Открываем позицию (мокаем проверки)
                        with patch.object(position_manager, '_get_ath_ratio', return_value=0.5):
                            with patch.object(position_manager, '_get_days_since_listing', return_value=10):
                                position = await position_manager.open_position(
                                    'BTC_USDT',
                                    50000.0,
                                    100.0,
                                )

                                # Проверяем
                                assert position is not None
                                assert 'BTC_USDT' in position_manager._active_positions

    @pytest.mark.asyncio
    async def test_open_position_max_limit(self, position_manager):
        """Тест лимита открытых позиций"""
        # Добавляем максимальное количество позиций
        for i in range(5):
            mock_position = Mock()
            mock_position.contract_symbol = f'SYMBOL_{i}'
            position_manager._active_positions[f'SYMBOL_{i}'] = mock_position

        # Пытаемся открыть еще одну
        with patch('src.trading.trader.config') as mock_config:
            mock_config.trading.max_positions = 5

            result = await position_manager.open_position(
                'NEW_SYMBOL',
                100.0,
                50.0,
            )

            assert result is None

    @pytest.mark.asyncio
    async def test_close_position(self, position_manager):
        """Тест закрытия позиции SHORT с прибылью"""
        # Создаем позицию SHORT
        mock_position = Mock()
        mock_position.contract_symbol = 'BTC_USDT'
        mock_position.entry_price = 50000.0
        mock_position.total_volume_usdt = 100.0
        mock_position.status = 'open'

        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch('src.trading.trader.db') as mock_db, \
             patch('src.trading.trader.config') as mock_config:
            mock_config.dry_run = True

            # Мокаем сессию
            mock_session = Mock()
            mock_db_position = Mock()
            mock_db_position.status = 'open'

            mock_session.query.return_value.filter.return_value.first.return_value = mock_db_position
            mock_session.commit = Mock()

            mock_db.get_session.return_value.__enter__.return_value = mock_session

            # Закрываем позицию SHORT с прибылью (цена упала)
            result = await position_manager.close_position(
                'BTC_USDT',
                49000.0,  # -2%
                reason='tp',
            )

            # Проверяем — close_position возвращает реальную цену fill (float)
            assert result is not None
            assert isinstance(result, float)
            assert 'BTC_USDT' not in position_manager._active_positions
            assert mock_db_position.status == 'closed'

    def test_should_close_position_tp_short(self, position_manager):
        """Тест сигнала TP для SHORT (цена упала на 2%)"""
        # Создаем позицию SHORT
        mock_position = Mock()
        mock_position.entry_price = 50000.0
        mock_position.opened_at = datetime.utcnow()
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch('src.trading.trader.config') as mock_config:
            mock_config.trading.take_profit_percentage = 2.0
            mock_config.trading.stop_loss_percentage = 0

            # Для SHORT TP когда цена упала на 2%
            result = position_manager.should_close_position('BTC_USDT', 49000.0)
            assert result == 'tp'

    def test_should_close_position_no_sl(self, position_manager):
        """Тест что SL не используется для SHORT"""
        # Создаем позицию SHORT
        mock_position = Mock()
        mock_position.entry_price = 50000.0
        mock_position.opened_at = datetime.utcnow()
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch('src.trading.trader.config') as mock_config:
            mock_config.trading.take_profit_percentage = 2.0
            mock_config.trading.stop_loss_percentage = 0
            mock_config.trading.position_timeout_hours = 720

            # Для SHORT SL не используется (цена выросла на 10%)
            result = position_manager.should_close_position('BTC_USDT', 55000.0)
            # Не должно быть SL
            assert result is None

    def test_should_close_position_timeout(self, position_manager):
        """Тест сигнала timeout"""
        # Создаем старую позицию
        mock_position = Mock()
        mock_position.entry_price = 50000.0
        mock_position.opened_at = datetime.utcnow() - timedelta(hours=721)  # 30 дней + 1 час
        mock_position.current_price = 50000.0
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch('src.trading.trader.config') as mock_config:
            mock_config.trading.take_profit_percentage = 2.0
            mock_config.trading.stop_loss_percentage = 0
            mock_config.trading.position_timeout_hours = 720  # 30 дней

            # Позиция старше 30 дней - должен быть timeout
            result = position_manager.should_close_position('BTC_USDT', 50000.0)
            assert result == 'timeout'

    def test_should_close_position_no_signal(self, position_manager):
        """Тест отсутствия сигнала"""
        # Создаем позицию SHORT
        mock_position = Mock()
        mock_position.entry_price = 50000.0
        mock_position.opened_at = datetime.utcnow()
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch('src.trading.trader.config') as mock_config:
            mock_config.trading.take_profit_percentage = 2.0
            mock_config.trading.stop_loss_percentage = 0
            mock_config.trading.position_timeout_hours = 720

            # Цена без изменений - не должно быть сигнала
            result = position_manager.should_close_position('BTC_USDT', 50000.0)
            assert result is None

    def test_should_add_averaging_first_level_short(self, position_manager):
        """Тест сигнала усреднения уровень 1 для SHORT (рост 300%)"""
        # Создаем позицию SHORT
        mock_position = Mock()
        mock_position.entry_price = 100.0
        mock_position.initial_entry_price = 100.0
        mock_position.avg_count = 0
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch.object(position_manager, '_get_trading_settings', return_value={
            'max_concurrent_coins': 10, 'max_avg_count': 3,
            'avg_levels': [300, 700, 1000], 'take_profit_pct': 2.0,
            'ath_ratio_threshold': 0.3, 'days_since_listing_limit': 30,
        }):
            # Цена выросла на 300% (с 100 до 400) - должен быть сигнал усреднения #1
            result = position_manager.should_add_averaging('BTC_USDT', 400.0)
            assert result is not None
            assert result[0] == 1  # Номер усреднения
            assert result[1] == 300  # Уровень

    def test_should_add_averaging_second_level_short(self, position_manager):
        """Тест сигнала усреднения уровень 2 для SHORT (рост 700%)"""
        # Создаем позицию SHORT
        mock_position = Mock()
        mock_position.entry_price = 100.0
        mock_position.initial_entry_price = 100.0
        mock_position.avg_count = 1  # Уже было 1 усреднение
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch.object(position_manager, '_get_trading_settings', return_value={
            'max_concurrent_coins': 10, 'max_avg_count': 3,
            'avg_levels': [300, 700, 1000], 'take_profit_pct': 2.0,
            'ath_ratio_threshold': 0.3, 'days_since_listing_limit': 30,
        }):
            # Мокаем проверку уровня - первый уровень (300) уже был, второй (700) еще нет
            # Метод принимает (symbol, level_pct), возвращаем True только для 300
            with patch.object(position_manager, '_averaging_level_used', side_effect=lambda s, lvl: lvl == 300):
                # Цена выросла на 700% (с 100 до 800) - должен быть сигнал усреднения #2
                result = position_manager.should_add_averaging('BTC_USDT', 800.0)
                assert result is not None
                assert result[0] == 2
                assert result[1] == 700

    def test_should_add_averaging_third_level_short(self, position_manager):
        """Тест сигнала усреднения уровень 3 для SHORT (рост 1000%)"""
        # Создаем позицию SHORT
        mock_position = Mock()
        mock_position.entry_price = 100.0
        mock_position.initial_entry_price = 100.0
        mock_position.avg_count = 2  # Уже было 2 усреднения
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch.object(position_manager, '_get_trading_settings', return_value={
            'max_concurrent_coins': 10, 'max_avg_count': 3,
            'avg_levels': [300, 700, 1000], 'take_profit_pct': 2.0,
            'ath_ratio_threshold': 0.3, 'days_since_listing_limit': 30,
        }):
            # Мокаем проверку уровня (чтобы не обращаться к БД)
            with patch.object(position_manager, '_averaging_level_used', side_effect=[True, True, False]):
                # Цена выросла на 1000% (с 100 до 1100) - должен быть сигнал усреднения #3
                result = position_manager.should_add_averaging('BTC_USDT', 1100.0)
                assert result is not None
                assert result[0] == 3
                assert result[1] == 1000

    def test_should_add_averaging_max_limit(self, position_manager):
        """Тест лимита усреднений"""
        # Создаем позицию с максимумом усреднений
        mock_position = Mock()
        mock_position.entry_price = 100.0
        mock_position.initial_entry_price = 100.0
        mock_position.avg_count = 3
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch('src.trading.trader.config') as mock_config:
            mock_config.trading.max_avg_count = 3
            mock_config.trading.avg_levels = [300, 700, 1000]

            # Даже если цена выросла - усреднений больше не будет
            result = position_manager.should_add_averaging('BTC_USDT', 2000.0)
            assert result is None

    def test_should_add_averaging_no_signal(self, position_manager):
        """Тест отсутствия сигнала усреднения"""
        # Создаем позицию SHORT
        mock_position = Mock()
        mock_position.entry_price = 100.0
        mock_position.initial_entry_price = 100.0
        mock_position.avg_count = 0
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch.object(position_manager, '_get_trading_settings', return_value={
            'max_concurrent_coins': 10, 'max_avg_count': 3,
            'avg_levels': [300, 700, 1000], 'take_profit_pct': 2.0,
            'ath_ratio_threshold': 0.3, 'days_since_listing_limit': 30,
        }):
            # Цена выросла только на 100% - недостаточно для усреднения (нужно 300%)
            result = position_manager.should_add_averaging('BTC_USDT', 200.0)
            assert result is None

    def test_can_reopen_true(self, position_manager):
        """Тест возможности переоткрытия - True"""
        mock_position = Mock()
        mock_position.entry_price = 100.0
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch.object(position_manager, '_get_ath_ratio', return_value=0.5):
            with patch.object(position_manager, '_get_days_since_listing', return_value=10):
                with patch('src.trading.trader.config') as mock_config:
                    mock_config.trading.ath_ratio_threshold = 0.3
                    mock_config.trading.days_since_listing_limit = 30

                    result = position_manager.can_reopen('BTC_USDT', 98.0)
                    assert result is True

    def test_can_reopen_false_ath_ratio(self, position_manager):
        """Тест возможности переоткрытия - False из-за ATH ratio"""
        mock_position = Mock()
        mock_position.entry_price = 100.0
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch.object(position_manager, '_get_ath_ratio', return_value=0.2):
            with patch.object(position_manager, '_get_days_since_listing', return_value=10):
                with patch('src.trading.trader.config') as mock_config:
                    mock_config.trading.ath_ratio_threshold = 0.3
                    mock_config.trading.days_since_listing_limit = 30

                    result = position_manager.can_reopen('BTC_USDT', 98.0)
                    assert result is False

    def test_can_reopen_false_days(self, position_manager):
        """Тест возможности переоткрытия - False из-за дней с листинга"""
        mock_position = Mock()
        mock_position.entry_price = 100.0
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch.object(position_manager, '_get_ath_ratio', return_value=0.5):
            with patch.object(position_manager, '_get_days_since_listing', return_value=35):
                with patch('src.trading.trader.config') as mock_config:
                    mock_config.trading.ath_ratio_threshold = 0.3
                    mock_config.trading.days_since_listing_limit = 30

                    result = position_manager.can_reopen('BTC_USDT', 98.0)
                    assert result is False

    @pytest.mark.asyncio
    async def test_add_averaging(self, position_manager):
        """Тест добавления усреднения"""
        # Создаем позицию SHORT
        mock_position = Mock()
        mock_position.id = 1
        mock_position.contract_symbol = 'BTC_USDT'
        mock_position.entry_price = 100.0
        mock_position.initial_entry_price = 100.0
        mock_position.total_volume_usdt = 100.0
        mock_position.avg_count = 0
        mock_position.status = 'open'

        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch('src.trading.trader.db') as mock_db, \
             patch('src.trading.trader.config') as mock_config:
            mock_config.dry_run = True

            # Мокаем сессию
            mock_session = Mock()
            mock_db_position = Mock()
            mock_db_position.id = 1
            mock_db_position.avg_count = 0
            mock_db_position.status = 'open'
            mock_db_position.entry_price = 100.0

            # Мокаем запросы
            mock_query = Mock()
            mock_session.query.return_value = mock_query
            mock_query.filter.return_value = mock_query
            mock_query.first.return_value = mock_db_position

            mock_session.commit = Mock()
            mock_session.add = Mock()
            mock_session.refresh = Mock()
            mock_session.expunge = Mock()

            mock_db.get_session.return_value.__enter__.return_value = mock_session

            # Мокаем настройки из БД
            with patch.object(position_manager, '_get_trading_settings', return_value={
                'max_concurrent_coins': 10, 'max_avg_count': 3,
                'avg_levels': [300, 700, 1000], 'take_profit_pct': 2.0,
                'ath_ratio_threshold': 0.3, 'days_since_listing_limit': 30,
            }):
                # Мокаем проверку уровня (уровень 300 еще не был)
                with patch.object(position_manager, '_averaging_level_used', return_value=False):
                    # Добавляем усреднение (рост цены на 300%)
                    result = await position_manager.add_averaging(
                        'BTC_USDT',
                        400.0,  # +300%
                        100.0,    # $100 USDT
                        1,        # Номер усреднения
                        300,      # Уровень
                    )

                    # Проверяем
                    assert result is True
                    assert mock_db_position.avg_count == 1

    @pytest.mark.asyncio
    async def test_update_position_price(self, position_manager):
        """Тест обновления цены позиции"""
        # Создаем позицию SHORT
        mock_position = Mock()
        mock_position.entry_price = 100.0
        mock_position.total_volume_usdt = 100.0
        position_manager._active_positions['BTC_USDT'] = mock_position

        with patch('src.trading.trader.db') as mock_db:
            # Мокаем сессию
            mock_session = Mock()
            mock_db_position = Mock()
            mock_db_position.current_price = 100.0
            mock_db_position.unrealized_pnl = 0.0

            mock_session.query.return_value.filter.return_value.first.return_value = mock_db_position
            mock_session.commit = Mock()

            mock_db.get_session.return_value.__enter__.return_value = mock_session

            # Обновляем цену (цена упала - прибыль для SHORT)
            result = await position_manager.update_position_price('BTC_USDT', 98.0)

            # Проверяем
            assert result is True
            assert mock_db_position.current_price == 98.0

    def test_get_all_positions(self, position_manager):
        """Тест получения всех позиций"""
        # Добавляем позиции
        mock_position1 = Mock()
        mock_position1.contract_symbol = 'BTC_USDT'
        position_manager._active_positions['BTC_USDT'] = mock_position1

        mock_position2 = Mock()
        mock_position2.contract_symbol = 'ETH_USDT'
        position_manager._active_positions['ETH_USDT'] = mock_position2

        # Получаем все
        all_positions = position_manager.get_all_positions()

        assert len(all_positions) == 2
        assert 'BTC_USDT' in all_positions
        assert 'ETH_USDT' in all_positions
