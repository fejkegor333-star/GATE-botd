"""
Режим разгона — плавное увеличение объёма позиции при успешной работе.

Логика:
- Счётчик последовательных успешных TP для каждого символа
- При каждом TP: multiplier += step_pct / 100  (до max_multiplier)
- При убытке или таймауте: сброс multiplier до 1.0
- Объём = base_volume * multiplier
"""
import logging
from typing import Dict

from src.db.connection import db

logger = logging.getLogger(__name__)


class AccelerationManager:
    """Менеджер режима разгона"""

    def __init__(self):
        # Множитель для каждого символа: symbol -> multiplier (1.0 = базовый)
        self._multipliers: Dict[str, float] = {}

    def _get_settings(self) -> tuple:
        """Получить настройки разгона из БД: (enabled, step_pct, max_multiplier)"""
        if not db._initialized:
            return False, 10.0, 3.0

        try:
            from src.db.settings import SettingsManager
            with db.get_session() as session:
                settings = SettingsManager(session)
                enabled = settings.get('acceleration_enabled', False)
                step_pct = settings.get('acceleration_step_pct', 10.0)
                max_mult = settings.get('acceleration_max_multiplier', 3.0)
                return enabled, step_pct, max_mult
        except Exception as e:
            logger.debug(f"Ошибка получения настроек разгона: {e}")
            return False, 10.0, 3.0

    def get_volume_multiplier(self, symbol: str) -> float:
        """
        Получить текущий множитель объёма для символа.

        Returns:
            Множитель (1.0 = базовый, >1.0 = разгон)
        """
        enabled, _, _ = self._get_settings()
        if not enabled:
            return 1.0
        return self._multipliers.get(symbol, 1.0)

    def calculate_volume(self, symbol: str, base_volume_usdt: float) -> float:
        """
        Рассчитать объём с учётом разгона.

        Args:
            symbol: Символ контракта
            base_volume_usdt: Базовый объём в USDT

        Returns:
            Объём с учётом множителя
        """
        multiplier = self.get_volume_multiplier(symbol)
        volume = base_volume_usdt * multiplier
        if multiplier > 1.0:
            logger.info(f"Разгон {symbol}: {base_volume_usdt:.2f} * {multiplier:.2f} = {volume:.2f} USDT")
        return volume

    def on_tp_close(self, symbol: str):
        """
        Вызывать при успешном TP закрытии — увеличиваем множитель.
        """
        enabled, step_pct, max_mult = self._get_settings()
        if not enabled:
            return

        current = self._multipliers.get(symbol, 1.0)
        new_mult = min(current + step_pct / 100, max_mult)
        self._multipliers[symbol] = new_mult
        logger.info(f"Разгон {symbol}: множитель {current:.2f} -> {new_mult:.2f} (TP)")

    def on_loss_close(self, symbol: str):
        """
        Вызывать при убыточном закрытии или таймауте — сбрасываем множитель.
        """
        if symbol in self._multipliers:
            old = self._multipliers.pop(symbol)
            logger.info(f"Разгон {symbol}: сброс множителя {old:.2f} -> 1.0 (убыток)")

    def get_all_multipliers(self) -> Dict[str, float]:
        """Получить все текущие множители"""
        return dict(self._multipliers)


# Глобальный инстанс
acceleration_manager = AccelerationManager()
