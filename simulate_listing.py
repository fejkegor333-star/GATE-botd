"""
Симуляция нового листинга для тестирования бота
Без реального API — эмулируем поведение
"""
import asyncio
import logging
from datetime import datetime

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)

logger = logging.getLogger(__name__)


async def simulate_new_listing():
    """Симулируем появление нового листинга"""
    symbol = "TEST_NEW_USDT"

    logger.info("=" * 60)
    logger.info("🚀 СИМУЛЯЦИЯ НОВОГО ЛИСТИНГА")
    logger.info("=" * 60)

    logger.info(f"Обнаружен новый контракт: {symbol}")
    logger.info(f"Launch time: {datetime.utcnow()}")
    logger.info("Ожидание заполнения стакана...")

    # Эмуляция заполнения стакана
    await asyncio.sleep(2)

    logger.info("✅ Стакан заполнен (ликвидность OK)")
    logger.info(f"Цена: $0.00100")
    logger.info("Объем: $50,000")

    logger.info("")
    logger.info("🟢 СИГНАЛ SHORT")
    logger.info(f"Открываем позицию: {symbol}")
    logger.info(f"Объем: $10 USDT")
    logger.info(f"Цена входа: $0.00100")

    await asyncio.sleep(2)

    # Эмуляция движения цены
    logger.info("")
    logger.info("📈 Цена выросла на 300%...")
    logger.info(f"Текущая цена: $0.00400")
    logger.info("📊 Усреднение #1 добавлено")

    await asyncio.sleep(2)

    logger.info("")
    logger.info("📉 Цена упала на 2%...")
    logger.info(f"Текущая цена: $0.00392")
    logger.info("✅ TEKE-PROFIT сработал!")
    logger.info(f"PnL: +$0.20 (+2%)")

    logger.info("")
    logger.info("🔄 Переоткрытие позиции...")
    logger.info(f"Новая цена входа: $0.00392")

    logger.info("")
    logger.info("=" * 60)
    logger.info("📊 РЕЗУЛЬТАТ СДЕЛКИ:")
    logger.info("=" * 60)
    logger.info("Монета: TEST_NEW_USDT")
    logger.info("Цена входа: $0.00100 → $0.00392 → $0.00384")
    logger.info("PnL: +$0.40 (+4%)")
    logger.info("Усреднений: 1")
    logger.info("=" * 60)


if __name__ == '__main__':
    asyncio.run(simulate_new_listing())
