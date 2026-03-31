"""
Модуль работы с настройками в БД
"""
import json
import logging
from typing import Any, Optional
from sqlalchemy.orm import Session

from src.db.models import Setting
from src.utils.config import config

logger = logging.getLogger(__name__)


class SettingsManager:
    """Менеджер настроек бота"""

    # Значения по умолчанию
    DEFAULT_SETTINGS = {
        'max_concurrent_coins': {
            'value': '10',
            'type': 'int',
            'description': 'Максимальное количество одновременно работающих монет (0 = остановка)'
        },
        'initial_position_usdt': {
            'value': '10',
            'type': 'float',
            'description': 'Начальный объём позиции в USDT'
        },
        'max_avg_count': {
            'value': '3',
            'type': 'int',
            'description': 'Максимальное количество усреднений'
        },
        'avg_amount_usdt': {
            'value': '10',
            'type': 'float',
            'description': 'Сумма усреднения в USDT'
        },
        'avg_levels': {
            'value': '[300, 700, 1000]',
            'type': 'json',
            'description': 'Уровни усреднений в процентах'
        },
        'take_profit_pct': {
            'value': '2',
            'type': 'float',
            'description': 'Процент тейк-профита'
        },
        'ath_ratio_threshold': {
            'value': '0.3',
            'type': 'float',
            'description': 'ATH ratio порог для входа'
        },
        'days_since_listing_limit': {
            'value': '30',
            'type': 'int',
            'description': 'Дней с листинга (макс. для работы)'
        },
        'min_liquidity_usdt': {
            'value': '1000',
            'type': 'float',
            'description': 'Минимальная ликвидность в стакане'
        },
        'max_drawdown_pct': {
            'value': '50',
            'type': 'float',
            'description': 'Дневной лимит просадки в %'
        },
        'protection_transfer_pct': {
            'value': '25',
            'type': 'float',
            'description': 'Процент перевода при защите от ликвидации'
        },
        'protection_trigger_pct': {
            'value': '50',
            'type': 'float',
            'description': 'Просадка для срабатывания защиты в %'
        },
        'acceleration_enabled': {
            'value': 'false',
            'type': 'bool',
            'description': 'Режим разгона (увеличение объёма при успехах)'
        },
        'acceleration_step_pct': {
            'value': '10',
            'type': 'float',
            'description': 'Шаг увеличения объёма в % за каждый успешный TP'
        },
        'acceleration_max_multiplier': {
            'value': '3.0',
            'type': 'float',
            'description': 'Максимальный множитель объёма (3x = макс. 30 USDT при базе 10)'
        },
        'whitelist_only': {
            'value': 'false',
            'type': 'bool',
            'description': 'Торговать только монеты из белого списка'
        },
        'filter_stablecoins': {
            'value': 'true',
            'type': 'bool',
            'description': 'Фильтровать стейблкоины и токенизированные акции'
        },
        'orderbook_monitoring_enabled': {
            'value': 'true',
            'type': 'bool',
            'description': 'Включить мониторинг стакана через WebSocket'
        },
        'orderbook_update_throttle_ms': {
            'value': '100',
            'type': 'int',
            'description': 'Минимальный интервал обработки обновлений стакана (мс)'
        },
        'check_orderbook_before_entry': {
            'value': 'false',
            'type': 'bool',
            'description': 'Проверять стакан перед входом в позицию (should_sell_signal)'
        },
    }

    def __init__(self, session: Session):
        self.session = session

    def get(self, param_name: str, default: Any = None) -> Any:
        """
        Получить значение параметра

        Args:
            param_name: Имя параметра
            default: Значение по умолчанию, если параметр не найден

        Returns:
            Значение параметра приведенное к типу
        """
        setting = self.session.query(Setting).filter(
            Setting.param_name == param_name
        ).first()

        if not setting:
            # Попробуем взять из default настроек
            if param_name in self.DEFAULT_SETTINGS:
                default_config = self.DEFAULT_SETTINGS[param_name]
                value = self._parse_value(default_config['value'], default_config['type'])
                return value
            return default

        return self._parse_value(setting.param_value, setting.param_type)

    def _invalidate_cache(self):
        """Сбросить Redis кэш настроек"""
        try:
            from src.cache.redis_client import redis_cache
            redis_cache.invalidate_settings()
        except Exception:
            pass

    def set(self, param_name: str, value: Any, updated_by: str = 'system') -> bool:
        """
        Установить значение параметра

        Args:
            param_name: Имя параметра
            value: Новое значение
            updated_by: Кто обновил (system, telegram)

        Returns:
            True если успешно, False если ошибка
        """
        try:
            # Определяем тип значения
            param_type = self._detect_type(value)

            # Преобразуем в строку
            if param_type == 'json':
                str_value = json.dumps(value)
            else:
                str_value = str(value)

            # Ищем существующую запись
            setting = self.session.query(Setting).filter(
                Setting.param_name == param_name
            ).first()

            if setting:
                # Обновляем
                setting.param_value = str_value
                setting.param_type = param_type
                setting.updated_by = updated_by
            else:
                # Создаем новую
                description = self.DEFAULT_SETTINGS.get(param_name, {}).get('description', '')
                setting = Setting(
                    param_name=param_name,
                    param_value=str_value,
                    param_type=param_type,
                    description=description,
                    updated_by=updated_by
                )
                self.session.add(setting)

            self.session.commit()
            self._invalidate_cache()
            logger.info(f"Параметр '{param_name}' обновлен: {value} (тип: {param_type})")
            return True

        except Exception as e:
            self.session.rollback()
            logger.error(f"Ошибка при установке параметра '{param_name}': {e}")
            return False

    def get_all(self) -> dict[str, Any]:
        """Получить все параметры как словарь"""
        result = {}

        # Получаем из БД
        settings = self.session.query(Setting).all()
        for setting in settings:
            result[setting.param_name] = self._parse_value(
                setting.param_value,
                setting.param_type
            )

        # Добавляем отсутствующие из default
        for param_name, config in self.DEFAULT_SETTINGS.items():
            if param_name not in result:
                result[param_name] = self._parse_value(config['value'], config['type'])

        return result

    def init_default_settings(self):
        """Инициализация настроек по умолчанию"""
        for param_name, config in self.DEFAULT_SETTINGS.items():
            existing = self.session.query(Setting).filter(
                Setting.param_name == param_name
            ).first()

            if not existing:
                setting = Setting(
                    param_name=param_name,
                    param_value=config['value'],
                    param_type=config['type'],
                    description=config['description'],
                    updated_by='system'
                )
                self.session.add(setting)
                logger.info(f"Создана настройка по умолчанию: {param_name}")

        self.session.commit()

    def _parse_value(self, value: str, param_type: str) -> Any:
        """Преобразовать строку в значение нужного типа"""
        try:
            if param_type == 'int':
                return int(value)
            elif param_type == 'float':
                return float(value)
            elif param_type == 'bool':
                return value.lower() in ('true', '1', 'yes')
            elif param_type == 'json':
                return json.loads(value)
            else:  # str
                return value
        except (ValueError, json.JSONDecodeError) as e:
            logger.error(f"Ошибка парсинга значения '{value}' (тип {param_type}): {e}")
            return value

    def _detect_type(self, value: Any) -> str:
        """Определить тип значения"""
        if isinstance(value, bool):
            return 'bool'
        elif isinstance(value, int):
            return 'int'
        elif isinstance(value, float):
            return 'float'
        elif isinstance(value, (list, dict)):
            return 'json'
        else:
            return 'str'
