"""
Модуль конфигурации бота
Загружает настройки из .env и config.yaml
"""
import os
import yaml
from pathlib import Path
from dataclasses import dataclass
from typing import List, Any, Optional
from dotenv import load_dotenv

# Загружаем .env
load_dotenv(override=True)


@dataclass
class DatabaseConfig:
    """Конфигурация базы данных"""
    host: str
    port: int
    name: str
    user: str
    password: str
    pool_size: int = 10
    max_overflow: int = 20
    pool_recycle: int = 3600

    @classmethod
    def from_env(cls) -> 'DatabaseConfig':
        return cls(
            host=os.getenv('DB_HOST', 'localhost'),
            port=int(os.getenv('DB_PORT', 5432)),
            name=os.getenv('DB_NAME', 'gate_bot'),
            user=os.getenv('DB_USER', 'gate_bot'),
            password=os.getenv('DB_PASSWORD', ''),
        )


@dataclass
class GateApiConfig:
    """Конфигурация Gate.io API"""
    api_key: str
    api_secret: str
    api_url: str
    ws_url: str
    testnet: bool = False
    timeout: int = 30

    @classmethod
    def from_env(cls) -> 'GateApiConfig':
        testnet = os.getenv('GATE_TESTNET', 'false').lower() == 'true'
        base_url = os.getenv('GATE_API_URL', 'https://api.gateio.ws')

        # Testnet URL если нужно
        if testnet:
            base_url = 'https://fx-api-testnet.gateio.ws'

        return cls(
            api_key=os.getenv('GATE_API_KEY', ''),
            api_secret=os.getenv('GATE_API_SECRET', ''),
            api_url=base_url,
            ws_url=os.getenv('GATE_WS_URL', 'wss://fx-ws.gateio.ws/v4/ws/usdt'),
            testnet=testnet,
            timeout=int(os.getenv('API_TIMEOUT', 30)),
        )


@dataclass
class TelegramConfig:
    """Конфигурация Telegram бота"""
    bot_token: str
    admin_ids: list  # Список ID администраторов

    @property
    def admin_id(self) -> int:
        """Основной админ (первый в списке) — для обратной совместимости"""
        return self.admin_ids[0] if self.admin_ids else 0

    def is_admin(self, user_id: int) -> bool:
        """Проверить является ли пользователь админом"""
        return user_id in self.admin_ids

    @classmethod
    def from_env(cls) -> 'TelegramConfig':
        raw = os.getenv('TELEGRAM_ADMIN_ID', '0')
        admin_ids = [int(x.strip()) for x in raw.split(',') if x.strip()]
        return cls(
            bot_token=os.getenv('TELEGRAM_BOT_TOKEN', ''),
            admin_ids=admin_ids,
        )


@dataclass
class MonitoringConfig:
    """Конфигурация модуля мониторинга"""
    poll_interval_seconds: int
    new_listing_timeout_minutes: int
    contracts_endpoint: str

    @classmethod
    def from_yaml(cls, yaml_data: dict) -> 'MonitoringConfig':
        monitoring = yaml_data.get('monitoring', {})
        return cls(
            poll_interval_seconds=monitoring.get('poll_interval_seconds', 5),
            new_listing_timeout_minutes=monitoring.get('new_listing_timeout_minutes', 10),
            contracts_endpoint=monitoring.get('contracts_endpoint', '/futures/usdt/contracts'),
        )


@dataclass
class RiskConfig:
    """Конфигурация риск-менеджмента"""
    balance_check_interval_seconds: int
    max_open_orders_per_second: int
    circuit_breaker_errors: int

    @classmethod
    def from_yaml(cls, yaml_data: dict) -> 'RiskConfig':
        risk = yaml_data.get('risk', {})
        return cls(
            balance_check_interval_seconds=risk.get('balance_check_interval_seconds', 5),
            max_open_orders_per_second=risk.get('max_open_orders_per_second', 10),
            circuit_breaker_errors=risk.get('circuit_breaker_errors', 3),
        )


@dataclass
class ATHConfig:
    """Конфигурация модуля ATH"""
    update_interval_hours: int
    lookback_weeks: int
    candles_endpoint: str

    @classmethod
    def from_yaml(cls, yaml_data: dict) -> 'ATHConfig':
        ath = yaml_data.get('ath', {})
        return cls(
            update_interval_hours=ath.get('update_interval_hours', 1),
            lookback_weeks=ath.get('lookback_weeks', 520),
            candles_endpoint=ath.get('candles_endpoint', '/futures/usdt/candlesticks'),
        )


@dataclass
class WebSocketConfig:
    """Конфигурация WebSocket модуля"""
    order_book_depth: int  # Глубина стакана
    reconnect_interval_seconds: int
    max_reconnect_attempts: int
    ping_interval_seconds: int
    order_book_update_interval_ms: int  # Интервал обновления стакана
    min_volume_threshold_usdt: float  # Минимальный объем для сигнала
    volume_imbalance_threshold: float  # Дисбаланс объема для сигнала
    min_order_book_levels: int  # Минимум уровней в стакане для сигнала
    max_spread_percentage: float  # Максимальный спред для сигнала (%)

    @classmethod
    def from_yaml(cls, yaml_data: dict) -> 'WebSocketConfig':
        ws = yaml_data.get('websocket', {})
        return cls(
            order_book_depth=ws.get('order_book_depth', 20),
            reconnect_interval_seconds=ws.get('reconnect_interval_seconds', 5),
            max_reconnect_attempts=ws.get('max_reconnect_attempts', 10),
            ping_interval_seconds=ws.get('ping_interval_seconds', 30),
            order_book_update_interval_ms=ws.get('order_book_update_interval_ms', 100),
            min_volume_threshold_usdt=ws.get('min_volume_threshold_usdt', 10000.0),
            volume_imbalance_threshold=ws.get('volume_imbalance_threshold', 0.6),
            min_order_book_levels=ws.get('min_order_book_levels', 5),
            max_spread_percentage=ws.get('max_spread_percentage', 1.0),
        )


@dataclass
class TradingConfig:
    """Конфигурация торгового модуля"""
    default_position_size_usdt: float  # Размер позиции в USDT
    default_leverage: int  # Плечо (1-100)
    max_positions: int  # Максимум открытых позиций
    max_avg_count: int  # Максимум усреднений

    # Сетка усреднений (в процентах от входа)
    avg_levels: list  # [300, 700, 1000] = рост 300%, 700%, 1000%

    # Стоп-лосс и тейк-профит
    stop_loss_percentage: float  # % от входа
    take_profit_percentage: float  # % от входа

    # Тайминги
    position_timeout_hours: int  # Закрытие позиции если не достигнута через N часов
    days_since_listing_limit: int  # Максимум дней с листинга для торговли

    # ATH ratio для входа
    ath_ratio_threshold: float  # Минимальный ATH ratio для входа

    @classmethod
    def from_yaml(cls, yaml_data: dict) -> 'TradingConfig':
        trading = yaml_data.get('trading', {})
        return cls(
            default_position_size_usdt=trading.get('default_position_size_usdt', 10.0),
            default_leverage=trading.get('default_leverage', 20),
            max_positions=trading.get('max_positions', 10),
            max_avg_count=trading.get('max_avg_count', 3),
            avg_levels=trading.get('avg_levels', [300, 700, 1000]),
            stop_loss_percentage=trading.get('stop_loss_percentage', 0),
            take_profit_percentage=trading.get('take_profit_percentage', 2.0),
            position_timeout_hours=trading.get('position_timeout_hours', 720),
            days_since_listing_limit=trading.get('days_since_listing_limit', 30),
            ath_ratio_threshold=trading.get('ath_ratio_threshold', 0.3),
        )


@dataclass
class RedisConfig:
    """Конфигурация Redis"""
    host: str
    port: int
    db: int
    password: str
    enabled: bool

    @classmethod
    def from_yaml(cls, yaml_data: dict) -> 'RedisConfig':
        redis = yaml_data.get('redis', {})
        return cls(
            host=os.getenv('REDIS_HOST', redis.get('host', 'localhost')),
            port=int(os.getenv('REDIS_PORT', redis.get('port', 6379))),
            db=int(redis.get('db', 0)),
            password=os.getenv('REDIS_PASSWORD', redis.get('password', '')),
            enabled=redis.get('enabled', False),
        )


class Config:
    """Основной класс конфигурации"""

    def __init__(self):
        # Загружаем из .env
        self.db = DatabaseConfig.from_env()
        self.gate = GateApiConfig.from_env()
        self.telegram = TelegramConfig.from_env()

        # Загружаем из YAML
        yaml_path = Path(__file__).parent.parent.parent / 'config' / 'config.yaml'
        self._load_yaml(yaml_path)

        # Общие настройки
        self.debug = os.getenv('DEBUG', 'false').lower() == 'true'
        self.dry_run = os.getenv('DRY_RUN', 'false').lower() == 'true'
        self.log_level = os.getenv('LOG_LEVEL', 'INFO')
        self.sentry_dsn = os.getenv('SENTRY_DSN', '')

    def _load_yaml(self, yaml_path: Path):
        """Загружает конфиг из YAML файла"""
        if not yaml_path.exists():
            # Используем значения по умолчанию
            self.monitoring = MonitoringConfig.from_yaml({})
            self.risk = RiskConfig.from_yaml({})
            self.ath = ATHConfig.from_yaml({})
            self.websocket = WebSocketConfig.from_yaml({})
            self.trading = TradingConfig.from_yaml({})
            self.redis = RedisConfig.from_yaml({})
            return

        with open(yaml_path, 'r', encoding='utf-8') as f:
            yaml_data = yaml.safe_load(f)

        self.monitoring = MonitoringConfig.from_yaml(yaml_data)
        self.risk = RiskConfig.from_yaml(yaml_data)
        self.ath = ATHConfig.from_yaml(yaml_data)
        self.websocket = WebSocketConfig.from_yaml(yaml_data)
        self.trading = TradingConfig.from_yaml(yaml_data)
        self.redis = RedisConfig.from_yaml(yaml_data)

    def validate(self) -> bool:
        """Валидация конфигурации"""
        errors = []

        # Проверка обязательных полей
        if not self.gate.api_key or self.gate.api_key == 'your_api_key_here':
            errors.append("GATE_API_KEY не установлен")

        if not self.gate.api_secret or self.gate.api_secret == 'your_api_secret_here':
            errors.append("GATE_API_SECRET не установлен")

        if not self.telegram.bot_token or self.telegram.bot_token == 'your_bot_token_here':
            errors.append("TELEGRAM_BOT_TOKEN не установлен")

        if not self.telegram.admin_ids or self.telegram.admin_ids == [0]:
            errors.append("TELEGRAM_ADMIN_ID не установлен")

        if errors:
            raise ValueError(f"Ошибки конфигурации:\n" + "\n".join(f"  - {e}" for e in errors))

        return True


# Глобальный инстанс конфига
config = Config()
