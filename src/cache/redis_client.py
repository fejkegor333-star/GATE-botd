"""
Redis клиент для кэширования и быстрых операций.
Gracefully деградирует если Redis недоступен — бот продолжает работать без кэша.
"""
import json
import logging
from typing import Any, Optional

from src.utils.config import config

logger = logging.getLogger(__name__)


class RedisCache:
    """
    Кэш на основе Redis.
    Если Redis недоступен — все операции возвращают None/False (no-op).
    """

    def __init__(self):
        self._client = None
        self._available = False

    def init(self):
        """Инициализировать подключение к Redis"""
        if not config.redis.enabled:
            logger.info("Redis отключен в конфигурации")
            return

        try:
            import redis
            self._client = redis.Redis(
                host=config.redis.host,
                port=config.redis.port,
                db=config.redis.db,
                password=config.redis.password or None,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            # Проверяем подключение
            self._client.ping()
            self._available = True
            logger.info(f"Redis подключен: {config.redis.host}:{config.redis.port}")
        except ImportError:
            logger.warning("redis пакет не установлен, кэширование отключено")
        except Exception as e:
            logger.warning(f"Redis недоступен: {e}, кэширование отключено")
            self._client = None

    def close(self):
        """Закрыть подключение"""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            self._available = False

    @property
    def is_available(self) -> bool:
        return self._available

    def get(self, key: str) -> Optional[str]:
        """Получить значение по ключу"""
        if not self._available:
            return None
        try:
            return self._client.get(key)
        except Exception as e:
            logger.debug(f"Redis get error: {e}")
            return None

    def set(self, key: str, value: str, ttl: int = 300) -> bool:
        """Установить значение с TTL (по умолчанию 5 минут)"""
        if not self._available:
            return False
        try:
            self._client.setex(key, ttl, value)
            return True
        except Exception as e:
            logger.debug(f"Redis set error: {e}")
            return False

    def delete(self, key: str) -> bool:
        """Удалить ключ"""
        if not self._available:
            return False
        try:
            self._client.delete(key)
            return True
        except Exception as e:
            logger.debug(f"Redis delete error: {e}")
            return False

    def get_json(self, key: str) -> Optional[Any]:
        """Получить JSON объект"""
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def set_json(self, key: str, value: Any, ttl: int = 300) -> bool:
        """Сохранить JSON объект"""
        try:
            return self.set(key, json.dumps(value), ttl)
        except (TypeError, ValueError):
            return False

    def get_settings(self) -> Optional[dict]:
        """Получить кэшированные настройки"""
        return self.get_json('bot:settings')

    def set_settings(self, settings: dict, ttl: int = 60) -> bool:
        """Кэшировать настройки (TTL 1 минута)"""
        return self.set_json('bot:settings', settings, ttl)

    def invalidate_settings(self):
        """Сбросить кэш настроек"""
        self.delete('bot:settings')

    def cache_contract_info(self, symbol: str, info: dict, ttl: int = 3600) -> bool:
        """Кэшировать информацию о контракте (TTL 1 час)"""
        return self.set_json(f'contract:{symbol}', info, ttl)

    def get_contract_info(self, symbol: str) -> Optional[dict]:
        """Получить кэшированную информацию о контракте"""
        return self.get_json(f'contract:{symbol}')


# Глобальный инстанс
redis_cache = RedisCache()
