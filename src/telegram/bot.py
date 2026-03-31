"""
Telegram бот для управления и уведомлений
Улучшенная версия с inline клавиатурой и расширенными функциями
"""
import asyncio
import logging
import csv
import io
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timedelta, date, timezone
from decimal import Decimal

# Московское время (UTC+3) для уведомлений
_MSK = timezone(timedelta(hours=3))


def _msk_now() -> datetime:
    """Текущее московское время"""
    return datetime.now(_MSK)

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.utils.config import config
from src.db.connection import db
from src.db.models import Position, Contract, Trade, SymbolList, SystemHealth
from src.trading.trader import position_manager
from src.risk.risk_manager import risk_manager

logger = logging.getLogger(__name__)


# ========================= CALLBACK DATA =========================
CALLBACK_SEPARATOR = "|"

def make_callback_data(action: str, *args) -> str:
    """Создать callback data"""
    return CALLBACK_SEPARATOR.join([action, *map(str, args)])

def parse_callback_data(data: str) -> Tuple[str, ...]:
    """Распарсить callback data"""
    return tuple(data.split(CALLBACK_SEPARATOR))


# ========================= KEYBOARDS =========================
class Keyboards:
    """Клавиатуры для бота"""

    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        """Главное меню"""
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="📊 Статус", callback_data=make_callback_data("status")),
            InlineKeyboardButton(text="💰 Баланс", callback_data=make_callback_data("balance")),
        )
        builder.row(
            InlineKeyboardButton(text="📈 Позиции", callback_data=make_callback_data("positions")),
            InlineKeyboardButton(text="📉 PnL", callback_data=make_callback_data("pnl")),
        )
        builder.row(
            InlineKeyboardButton(text="⚙️ Настройки", callback_data=make_callback_data("settings_menu")),
            InlineKeyboardButton(text="📋 Контракты", callback_data=make_callback_data("contracts")),
        )
        builder.row(
            InlineKeyboardButton(text="📜 Сделки", callback_data=make_callback_data("trades")),
            InlineKeyboardButton(text="🏥 Здоровье", callback_data=make_callback_data("health")),
        )
        builder.row(
            InlineKeyboardButton(text="🚫/✅ Списки", callback_data=make_callback_data("lists_menu")),
            InlineKeyboardButton(text="📊 Статистика", callback_data=make_callback_data("stats")),
        )
        builder.row(
            InlineKeyboardButton(text="📥 Экспорт", callback_data=make_callback_data("export_menu")),
            InlineKeyboardButton(text="🔔 Уведомления", callback_data=make_callback_data("notifications")),
        )
        builder.row(
            InlineKeyboardButton(text="🛑 Остановить", callback_data=make_callback_data("stop_trading")),
            InlineKeyboardButton(text="▶️ Запуск", callback_data=make_callback_data("start_trading")),
        )
        return builder.as_markup()

    @staticmethod
    def reply_keyboard() -> ReplyKeyboardMarkup:
        """Постоянная клавиатура внизу экрана"""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📊 Статус"), KeyboardButton(text="📈 Позиции"), KeyboardButton(text="💰 Баланс")],
                [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="📉 PnL"), KeyboardButton(text="📜 Сделки")],
                [KeyboardButton(text="🏥 Здоровье"), KeyboardButton(text="📋 Контракты"), KeyboardButton(text="📊 Стат")],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def settings_menu() -> InlineKeyboardMarkup:
        """Меню настроек"""
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="💵 Объем позиции", callback_data=make_callback_data("setting_edit", "position_size")),
            InlineKeyboardButton(text="🎯 TP %", callback_data=make_callback_data("setting_edit", "tp_pct")),
        )
        builder.row(
            InlineKeyboardButton(text="📊 ATH ratio", callback_data=make_callback_data("setting_edit", "ath_ratio")),
            InlineKeyboardButton(text="📅 Дней листинг", callback_data=make_callback_data("setting_edit", "days")),
        )
        builder.row(
            InlineKeyboardButton(text="🔢 Максимум монет", callback_data=make_callback_data("setting_edit", "max_coins")),
            InlineKeyboardButton(text="📉 Просадка %", callback_data=make_callback_data("setting_edit", "drawdown")),
        )
        builder.row(
            InlineKeyboardButton(text="📊 Усреднения", callback_data=make_callback_data("setting_edit", "avg_levels")),
            InlineKeyboardButton(text="💳 Защита баланса", callback_data=make_callback_data("setting_edit", "protection")),
        )
        builder.row(
            InlineKeyboardButton(text="🔢 Макс. усреднений", callback_data=make_callback_data("setting_edit", "max_avg_count")),
        )
        builder.row(
            InlineKeyboardButton(text="🚀 Разгон", callback_data=make_callback_data("setting_edit", "acceleration")),
            InlineKeyboardButton(text="📡 Стакан", callback_data=make_callback_data("setting_edit", "orderbook")),
        )
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=make_callback_data("main")))
        return builder.as_markup()

    @staticmethod
    def position_actions(symbol: str) -> InlineKeyboardMarkup:
        """Кнопки действий для позиции"""
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="📊 Детали", callback_data=make_callback_data("position_detail", symbol)),
            InlineKeyboardButton(text="🔄 Добавить усреднение", callback_data=make_callback_data("position_avg", symbol)),
        )
        builder.row(
            InlineKeyboardButton(text="❌ Закрыть", callback_data=make_callback_data("position_close", symbol)),
            InlineKeyboardButton(text="⬅️ Назад", callback_data=make_callback_data("positions")),
        )
        return builder.as_markup()

    @staticmethod
    def setting_edit(param: str, current_value: str) -> InlineKeyboardMarkup:
        """Клавиатура редактирования настройки"""
        builder = InlineKeyboardBuilder()

        if param in ["position_size", "max_coins", "drawdown", "days", "avg_amount"]:
            # Числовые значения +/- шаг
            step = 5 if param in ("position_size", "avg_amount") else 1
            builder.row(
                InlineKeyboardButton(text=f"-{step}", callback_data=make_callback_data("setting_change", param, f"-{step}")),
                InlineKeyboardButton(text=f"+{step}", callback_data=make_callback_data("setting_change", param, f"+{step}")),
            )
            builder.row(
                InlineKeyboardButton(text="-10", callback_data=make_callback_data("setting_change", param, "-10")),
                InlineKeyboardButton(text="+10", callback_data=make_callback_data("setting_change", param, "+10")),
            )
        elif param == "max_avg_count":
            builder.row(
                InlineKeyboardButton(text="-1", callback_data=make_callback_data("setting_change", param, "-1")),
                InlineKeyboardButton(text="+1", callback_data=make_callback_data("setting_change", param, "+1")),
            )
        elif param == "tp_pct":
            builder.row(
                InlineKeyboardButton(text="-0.5%", callback_data=make_callback_data("setting_change", param, "-0.5")),
                InlineKeyboardButton(text="+0.5%", callback_data=make_callback_data("setting_change", param, "+0.5")),
            )
        elif param == "ath_ratio":
            builder.row(
                InlineKeyboardButton(text="-0.1", callback_data=make_callback_data("setting_change", param, "-0.1")),
                InlineKeyboardButton(text="+0.1", callback_data=make_callback_data("setting_change", param, "+0.1")),
            )

        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=make_callback_data("settings_menu")))
        return builder.as_markup()

    @staticmethod
    def lists_menu(whitelist_only: bool = False) -> InlineKeyboardMarkup:
        """Меню списков монет"""
        builder = InlineKeyboardBuilder()
        # Кнопка переключения режима белого списка
        wl_text = "✅ Белый список: ВКЛ" if whitelist_only else "⬜ Белый список: ВЫКЛ"
        builder.row(InlineKeyboardButton(text=wl_text, callback_data=make_callback_data("whitelist_toggle")))
        builder.row(
            InlineKeyboardButton(text="🚫 Черный список", callback_data=make_callback_data("blacklist")),
            InlineKeyboardButton(text="✅ Белый список", callback_data=make_callback_data("whitelist")),
        )
        builder.row(
            InlineKeyboardButton(text="➕ В черный", callback_data=make_callback_data("list_add", "blacklist")),
            InlineKeyboardButton(text="➕ В белый", callback_data=make_callback_data("list_add", "whitelist")),
        )
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=make_callback_data("main")))
        return builder.as_markup()

    @staticmethod
    def trades_filter() -> InlineKeyboardMarkup:
        """Фильтр для сделок"""
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="📊 Все", callback_data=make_callback_data("trades", "all")),
            InlineKeyboardButton(text="📈 Прибыльные", callback_data=make_callback_data("trades", "profit")),
            InlineKeyboardButton(text="📉 Убыточные", callback_data=make_callback_data("trades", "loss")),
        )
        builder.row(
            InlineKeyboardButton(text="📅 Сегодня", callback_data=make_callback_data("trades", "today")),
            InlineKeyboardButton(text="📅 Неделя", callback_data=make_callback_data("trades", "week")),
        )
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=make_callback_data("main")))
        return builder.as_markup()

    @staticmethod
    def export_menu() -> InlineKeyboardMarkup:
        """Меню экспорта"""
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="📊 Сделки CSV", callback_data=make_callback_data("export", "trades")),
            InlineKeyboardButton(text="📈 Позиции CSV", callback_data=make_callback_data("export", "positions")),
        )
        builder.row(
            InlineKeyboardButton(text="📋 Статистика", callback_data=make_callback_data("export", "stats")),
            InlineKeyboardButton(text="🔄 Настройки", callback_data=make_callback_data("export", "settings")),
        )
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=make_callback_data("main")))
        return builder.as_markup()

    @staticmethod
    def confirm_action(action: str, symbol: str = "") -> InlineKeyboardMarkup:
        """Подтверждение действия"""
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="✅ Да", callback_data=make_callback_data("confirm", action, symbol)),
            InlineKeyboardButton(text="❌ Отмена", callback_data=make_callback_data("cancel", symbol)),
        )
        return builder.as_markup()

    @staticmethod
    def notifications_toggle(enabled: bool) -> InlineKeyboardMarkup:
        """Переключатель уведомлений"""
        status = "✅ ВКЛ" if enabled else "❌ ВЫКЛ"
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text=status, callback_data=make_callback_data("notif_toggle")),
        )
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=make_callback_data("main")))
        return builder.as_markup()


# ========================= NOTIFIER =========================
class TelegramNotifier:
    """Отправка уведомлений в Telegram"""

    def __init__(self, bot: Bot):
        self.bot = bot
        self._enabled = True

    async def send_new_listing(self, symbol: str, launch_time: datetime):
        """Уведомление о новом листинге"""
        if not self._enabled:
            return

        message = (
            f"🚀 <b>Новый листинг!</b>\n\n"
            f"Монета: <code>{symbol}</code>\n"
            f"Запуск: {launch_time.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            f"Статус: Мониторинг стакана..."
        )

        await self._send_to_admin(message)

    async def send_position_opened(
        self,
        symbol: str,
        entry_price: float,
        volume_usdt: float,
        leverage: int = 20,
    ):
        """Уведомление об открытии позиции"""
        if not self._enabled:
            return

        message = (
            f"💰 <b>Позиция открыта</b>\n\n"
            f"Монета: <code>{symbol}</code>\n"
            f"Цена входа: ${entry_price:.4f}\n"
            f"Объем: ${volume_usdt:.2f}\n"
            f"Leverage: x{leverage}\n"
            f"Время: {_msk_now().strftime('%H:%M:%S')}"
        )

        await self._send_to_admin(message)

    async def send_position_closed(
        self,
        symbol: str,
        exit_price: float,
        pnl_usdt: float,
        pnl_pct: float,
        reason: str,
        duration: str = "",
        total_volume: float = 0,
    ):
        """Уведомление о закрытии позиции"""
        if not self._enabled:
            return

        emoji = "📈" if pnl_usdt > 0 else "📉"
        reason_emoji = {
            'tp': '✅ TP',
            'sl': '❌ SL',
            'timeout': '⏰ TIMEOUT',
            'manual': '👤 Вручную',
        }.get(reason, reason)

        message = (
            f"{emoji} <b>Позиция закрыта</b>\n\n"
            f"Монета: <code>{symbol}</code>\n"
            f"Цена выхода: ${exit_price:.4f}\n"
            f"PnL: ${pnl_usdt:+.2f} ({pnl_pct:+.2f}%)\n"
            f"Причина: {reason_emoji}\n"
        )
        if total_volume > 0:
            message += f"Общий объем: ${total_volume:.2f}\n"
        if duration:
            message += f"Длительность: {duration}\n"
        message += f"Время: {_msk_now().strftime('%H:%M:%S')}"

        await self._send_to_admin(message)

    async def send_averaging_added(
        self,
        symbol: str,
        avg_number: int,
        avg_price: float,
        avg_volume: float,
        new_avg_price: float,
    ):
        """Уведомление об усреднении"""
        if not self._enabled:
            return

        message = (
            f"📊 <b>Усреднение #{avg_number}</b>\n\n"
            f"Монета: <code>{symbol}</code>\n"
            f"Цена усреднения: ${avg_price:.4f}\n"
            f"Объем: ${avg_volume:.2f}\n"
            f"Новая средняя: ${new_avg_price:.4f}\n"
            f"Время: {_msk_now().strftime('%H:%M:%S')}"
        )

        await self._send_to_admin(message)

    async def send_position_reopened(
        self,
        symbol: str,
        entry_price: float,
        volume_usdt: float,
    ):
        """Уведомление о переоткрытии позиции"""
        if not self._enabled:
            return

        message = (
            f"🔄 <b>Позиция переоткрыта</b>\n\n"
            f"Монета: <code>{symbol}</code>\n"
            f"Цена входа: ${entry_price:.4f}\n"
            f"Объем: ${volume_usdt:.2f}\n"
            f"Время: {_msk_now().strftime('%H:%M:%S')}"
        )

        await self._send_to_admin(message)

    async def send_balance_transfer(
        self,
        from_account: str,
        to_account: str,
        amount: float,
        reason: str,
    ):
        """Уведомление о переводе баланса"""
        if not self._enabled:
            return

        account_names = {
            'spot': 'Спот',
            'futures': 'Фьючерсы',
        }

        message = (
            f"💰 <b>Защитный перевод</b>\n\n"
            f"Откуда: {account_names.get(from_account, from_account)}\n"
            f"Куда: {account_names.get(to_account, to_account)}\n"
            f"Сумма: ${amount:.2f} USDT\n"
            f"Причина: {reason}\n"
            f"Время: {_msk_now().strftime('%H:%M:%S')}"
        )

        await self._send_to_admin(message)

    async def send_listing_waiting(self, symbol: str, reason: str = ""):
        """Уведомление об ожидании начала торгов (не ошибка)"""
        if not self._enabled:
            return

        message = (
            f"⏳ <b>Ожидание торгов</b>\n\n"
            f"Монета: <code>{symbol}</code>\n"
            f"Контракт опубликован, но торги ещё не начались.\n"
            f"Бот ожидает начала торгов для открытия позиции.\n"
        )
        if reason:
            message += f"Детали: {reason}\n"
        message += f"Время: {_msk_now().strftime('%Y-%m-%d %H:%M:%S')}"

        await self._send_to_admin(message)

    async def send_error(self, error_message: str):
        """Уведомление об ошибке"""
        message = (
            f"⚠️ <b>Ошибка</b>\n\n"
            f"{error_message}\n"
            f"Время: {_msk_now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        await self._send_to_admin(message)

    async def send_daily_report(self):
        """Дневной отчет"""
        if not self._enabled:
            return

        try:
            with db.get_session() as session:
                today = datetime.utcnow().date()

                trades = session.query(Trade).filter(
                    Trade.created_at >= datetime.combine(today, datetime.min.time())
                ).all()

                total_pnl = sum(t.pnl or 0 for t in trades)
                total_trades = len(trades)
                winning_trades = sum(1 for t in trades if (t.pnl or 0) > 0)
                losing_trades = sum(1 for t in trades if (t.pnl or 0) < 0)

                active_positions = session.query(Position).filter(
                    Position.status == 'open'
                ).count()

            message = (
                f"📊 <b>Дневной отчет</b>\n\n"
                f"Дата: {today.strftime('%Y-%m-%d')}\n\n"
                f"<b>Торговля:</b>\n"
                f"Сделок: {total_trades}\n"
                f"Прибыльных: {winning_trades}\n"
                f"Убыточных: {losing_trades}\n"
                f"PnL: ${total_pnl:+.2f}\n\n"
                f"<b>Активные позиции:</b> {active_positions}\n\n"
                f"<b>Баланс риска:</b> ${risk_manager.get_daily_pnl():+.2f}"
            )

            await self._send_to_admin(message)

        except Exception as e:
            logger.error(f"Ошибка отправки дневного отчета: {e}")

    async def send_weekly_report(self):
        """Недельный отчет"""
        if not self._enabled:
            return

        try:
            with db.get_session() as session:
                week_ago = datetime.utcnow() - timedelta(days=7)

                trades = session.query(Trade).filter(
                    Trade.created_at >= week_ago
                ).all()

                total_pnl = sum(t.pnl or 0 for t in trades)
                total_trades = len(trades)
                winning_trades = sum(1 for t in trades if (t.pnl or 0) > 0)
                losing_trades = sum(1 for t in trades if (t.pnl or 0) < 0)

                win_rate = winning_trades / total_trades * 100 if total_trades > 0 else 0

                # Лучшие и худшие сделки
                sorted_trades = sorted([t for t in trades if t.pnl], key=lambda x: x.pnl, reverse=True)
                best = {'symbol': sorted_trades[0].contract_symbol, 'pnl': float(sorted_trades[0].pnl)} if sorted_trades else None
                worst = {'symbol': sorted_trades[-1].contract_symbol, 'pnl': float(sorted_trades[-1].pnl)} if sorted_trades else None

            message = (
                f"📊 <b>Недельный отчет</b>\n\n"
                f"Период: {(datetime.utcnow() - timedelta(days=7)).strftime('%Y-%m-%d')} - {_msk_now().strftime('%Y-%m-%d')}\n\n"
                f"<b>Торговля:</b>\n"
                f"Сделок: {total_trades}\n"
                f"Прибыльных: {winning_trades}\n"
                f"Убыточных: {losing_trades}\n"
                f"Win Rate: {win_rate:.1f}%\n"
                f"PnL: ${total_pnl:+.2f}\n\n"
            )

            if best:
                message += f"<b>Лучшая сделка:</b> {best['symbol']} +${best['pnl']:.2f}\n"
            if worst:
                message += f"<b>Худшая сделка:</b> {worst['symbol']} ${worst['pnl']:.2f}\n"

            await self._send_to_admin(message)

        except Exception as e:
            logger.error(f"Ошибка отправки недельного отчета: {e}")

    async def _send_to_admin(self, message: str):
        """Отправить сообщение всем админам"""
        for admin_id in config.telegram.admin_ids:
            try:
                await self.bot.send_message(
                    admin_id,
                    message,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления админу {admin_id}: {e}")

    def enable(self):
        """Включить уведомления"""
        self._enabled = True

    def disable(self):
        """Выключить уведомления"""
        self._enabled = False


# ========================= HELPERS =========================
class BotHelpers:
    """Вспомогательные функции для бота"""

    @staticmethod
    def get_setting_value(param: str) -> any:
        """Получить значение настройки"""
        try:
            from src.db.settings import SettingsManager
            with db.get_session() as session:
                settings = SettingsManager(session)
                return settings.get(param)
        except Exception as e:
            logger.error(f"Ошибка получения настройки {param}: {e}")
            return None

    @staticmethod
    def set_setting_value(param: str, value: any) -> bool:
        """Установить значение настройки"""
        try:
            from src.db.settings import SettingsManager
            with db.get_session() as session:
                settings = SettingsManager(session)
                settings.set(param, value)
                session.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка установки настройки {param}: {e}")
            return False

    @staticmethod
    def format_price(price: float) -> str:
        """Форматировать цену"""
        if price >= 1000:
            return f"${price:.2f}"
        elif price >= 1:
            return f"${price:.4f}"
        else:
            return f"${price:.8f}"

    @staticmethod
    def is_symbol_in_list(symbol: str, list_type: str) -> bool:
        """Проверить наличие символа в списке"""
        try:
            with db.get_session() as session:
                item = session.query(SymbolList).filter(
                    SymbolList.symbol == symbol,
                    SymbolList.list_type == list_type
                ).first()
                return item is not None
        except Exception as e:
            logger.error(f"Ошибка проверки списка: {e}")
            return False

    @staticmethod
    def get_symbol_list(list_type: str, limit: int = 20) -> List[dict]:
        """Получить список символов (возвращает словари чтобы избежать detached objects)"""
        try:
            with db.get_session() as session:
                items = session.query(SymbolList).filter(
                    SymbolList.list_type == list_type
                ).order_by(SymbolList.added_at.desc()).limit(limit).all()
                return [
                    {'symbol': item.symbol, 'reason': item.reason, 'added_at': item.added_at}
                    for item in items
                ]
        except Exception as e:
            logger.error(f"Ошибка получения списка: {e}")
            return []

    @staticmethod
    def add_to_symbol_list(symbol: str, list_type: str, reason: str = "") -> bool:
        """Добавить символ в список"""
        try:
            with db.get_session() as session:
                # Проверяем наличие
                existing = session.query(SymbolList).filter(
                    SymbolList.symbol == symbol,
                    SymbolList.list_type == list_type
                ).first()

                if existing:
                    return False

                item = SymbolList(
                    symbol=symbol,
                    list_type=list_type,
                    reason=reason,
                    added_by='telegram'
                )
                session.add(item)
                session.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка добавления в список: {e}")
            return False

    @staticmethod
    def remove_from_symbol_list(symbol: str, list_type: str) -> bool:
        """Удалить символ из списка"""
        try:
            with db.get_session() as session:
                item = session.query(SymbolList).filter(
                    SymbolList.symbol == symbol,
                    SymbolList.list_type == list_type
                ).first()

                if item:
                    session.delete(item)
                    session.commit()
                    return True
                return False
        except Exception as e:
            logger.error(f"Ошибка удаления из списка: {e}")
            return False

    @staticmethod
    def get_system_health() -> Dict[str, any]:
        """Получить здоровье системы"""
        try:
            from src.bot.core import get_trading_bot
            from src.api.websocket_client import ws_client

            trading_bot = get_trading_bot()

            status = {
                'bot_running': trading_bot.is_running() if trading_bot else False,
                'ws_connected': ws_client.is_connected() if hasattr(ws_client, 'is_connected') else False,
                'db_ok': True,
                'api_ok': True,
            }

            # Проверяем БД
            try:
                with db.get_session() as session:
                    session.query(Contract).count()
            except Exception:
                status['db_ok'] = False

            return status
        except Exception as e:
            logger.error(f"Ошибка получения здоровья системы: {e}")
            return {}

    @staticmethod
    async def close_position_manual(symbol: str) -> Tuple[bool, str]:
        """Закрыть позицию вручную"""
        try:
            position = position_manager.get_position(symbol)
            if not position:
                return False, "Позиция не найдена"

            # Текущая цена
            current_price = position.current_price or position.entry_price

            success = await position_manager.close_position(
                symbol,
                current_price,
                'manual'
            )

            if success:
                # Записываем PnL
                entry = float(position.entry_price)
                current = float(current_price)
                volume = float(position.total_volume_usdt)
                pnl_pct = (entry - current) / entry * 100
                pnl_usdt = volume * pnl_pct / 100
                await risk_manager.record_trade_pnl(pnl_usdt)

                return True, f"Позиция закрыта: PnL ${pnl_usdt:+.2f}"
            else:
                return False, "Ошибка закрытия позиции"

        except Exception as e:
            logger.error(f"Ошибка ручного закрытия: {e}")
            return False, str(e)

    @staticmethod
    def export_trades_csv(filter_type: str = "all") -> Optional[bytes]:
        """Экспорт сделок в CSV"""
        try:
            import io
            output = io.StringIO()
            writer = csv.writer(output)

            # Заголовки
            writer.writerow(['Дата', 'Символ', 'Тип', 'Цена', 'Объем', 'PnL', 'Комиссия'])

            with db.get_session() as session:
                query = session.query(Trade)

                if filter_type == "today":
                    start = datetime.combine(date.today(), datetime.min.time())
                    query = query.filter(Trade.created_at >= start)
                elif filter_type == "week":
                    start = datetime.utcnow() - timedelta(days=7)
                    query = query.filter(Trade.created_at >= start)
                elif filter_type == "profit":
                    query = query.filter(Trade.pnl > 0)
                elif filter_type == "loss":
                    query = query.filter(Trade.pnl < 0)

                trades = query.order_by(Trade.created_at.desc()).limit(1000).all()

                for trade in trades:
                    writer.writerow([
                        trade.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                        trade.contract_symbol,
                        trade.trade_type,
                        float(trade.price),
                        float(trade.volume_usdt),
                        float(trade.pnl) if trade.pnl else '',
                        float(trade.fee) if trade.fee else '',
                    ])

            output.seek(0)
            return output.getvalue().encode('utf-8')

        except Exception as e:
            logger.error(f"Ошибка экспорта сделок: {e}")
            return None

    @staticmethod
    def export_positions_csv() -> Optional[bytes]:
        """Экспорт позиций в CSV"""
        try:
            import io
            output = io.StringIO()
            writer = csv.writer(output)

            writer.writerow(['Символ', 'Цена входа', 'Текущая цена', 'Объем', 'Усреднений', 'PnL', 'Статус', 'Открыта'])

            with db.get_session() as session:
                positions = session.query(Position).all()

                for pos in positions:
                    pnl_pct = 0
                    if pos.current_price and pos.entry_price:
                        pnl_pct = (float(pos.entry_price) - float(pos.current_price)) / float(pos.entry_price) * 100

                    writer.writerow([
                        pos.contract_symbol,
                        float(pos.entry_price),
                        float(pos.current_price) if pos.current_price else '',
                        float(pos.total_volume_usdt),
                        pos.avg_count,
                        f"{pnl_pct:.2f}%",
                        pos.status,
                        pos.opened_at.strftime('%Y-%m-%d %H:%M:%S') if pos.opened_at else '',
                    ])

            output.seek(0)
            return output.getvalue().encode('utf-8')

        except Exception as e:
            logger.error(f"Ошибка экспорта позиций: {e}")
            return None


# ========================= TELEGRAM BOT =========================
class TelegramBot:
    """Telegram бот для управления"""

    def __init__(self):
        self.bot = Bot(token=config.telegram.bot_token)
        self.dp = Dispatcher()
        self.notifier = TelegramNotifier(self.bot)
        self._running = False
        self.helpers = BotHelpers()
        # Ожидание ввода для добавления в список: user_id -> list_type
        self._waiting_list_add: Dict[int, str] = {}

        # Регистрируем обработчики
        self._register_handlers()

    def _register_handlers(self):
        """Зарегистрировать обработчики команд"""

        @self.dp.message(Command("start"))
        async def cmd_start(message: Message):
            """Команда /start"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            # Быстрая сводка
            positions = position_manager.get_all_positions()
            pnl = risk_manager.get_daily_pnl()
            balance = await risk_manager.balance_checker.get_balance()

            summary = "📍 "
            summary += f"Позиций: {len(positions)}"
            summary += f" | PnL: ${pnl:+.2f}"
            if balance is not None:
                summary += f" | Баланс: ${balance:.2f}"

            help_text = (
                f"🤖 <b>Gate Futures Bot</b>\n\n"
                f"{summary}\n\n"
                "<b>Используйте кнопки внизу экрана для быстрого доступа</b>\n"
                "Или нажмите кнопки inline-меню ниже:"
            )

            # Отправляем reply keyboard (всегда внизу)
            await message.answer(help_text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.reply_keyboard())
            # Отправляем inline menu отдельным сообщением
            await message.answer("📋 <b>Inline-меню:</b>", parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())

        @self.dp.message(Command("help"))
        async def cmd_help(message: Message):
            """Команда /help"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            help_text = (
                "📚 <b>Справка</b>\n\n"
                "<b>Кнопки внизу экрана:</b>\n"
                "Быстрый доступ к основным функциям\n\n"
                "<b>Основные функции:</b>\n"
                "• 📊 Статус - состояние бота и систем\n"
                "• 💰 Баланс - текущий баланс счета\n"
                "• 📈 Позиции - открытые позиции с управлением\n"
                "• 📉 PnL - прибыль/убыток за день\n"
                "• ⚙️ Настройки - изменение параметров\n"
                "• 📋 Контракты - монеты в работе\n"
                "• 📜 Сделки - история торговых операций\n"
                "• 🏥 Здоровье - состояние систем\n\n"
                "<b>Управление:</b>\n"
                "• 🛑 Остановить - остановить торговлю\n"
                "• ▶️ Запуск - возобновить торговлю\n"
                "• 🔔 Уведомления - вкл/выкл уведомления\n\n"
                "<b>Списки:</b>\n"
                "• 🚫 Черный список - игнорируемые монеты\n"
                "• ✅ Белый список - приоритетные монеты\n\n"
                "<b>Экспорт:</b>\n"
                "• 📥 CSV экспорт сделок и позиций"
            )

            await message.answer(help_text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.reply_keyboard())

        @self.dp.message(Command("status"))
        async def cmd_status(message: Message):
            """Команда /status"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_status(message)

        @self.dp.message(Command("positions"))
        async def cmd_positions(message: Message):
            """Команда /positions"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_positions(message)

        @self.dp.message(Command("balance"))
        async def cmd_balance(message: Message):
            """Команда /balance"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_balance(message)

        @self.dp.message(Command("pnl"))
        async def cmd_pnl(message: Message):
            """Команда /pnl"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_pnl(message)

        @self.dp.message(Command("settings"))
        async def cmd_settings(message: Message):
            """Команда /settings"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_settings(message)

        @self.dp.message(Command("stats"))
        async def cmd_stats(message: Message):
            """Команда /stats"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_stats(message)

        @self.dp.message(Command("contracts"))
        async def cmd_contracts(message: Message):
            """Команда /contracts"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_contracts(message)

        @self.dp.message(Command("trades"))
        async def cmd_trades(message: Message):
            """Команда /trades"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_trades(message, "all")

        @self.dp.message(Command("notifications"))
        async def cmd_notifications(message: Message):
            """Команда /notifications"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_notifications(message)

        @self.dp.message(Command("health"))
        async def cmd_health(message: Message):
            """Команда /health"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_health(message)

        @self.dp.message(Command("stop"))
        async def cmd_stop(message: Message):
            """Команда /stop — остановка торговли"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            self.helpers.set_setting_value('max_concurrent_coins', 0)
            await message.answer(
                "🛑 <b>Торговля остановлена</b>\n\n"
                "max_concurrent_coins = 0\n"
                "Новые позиции открываться не будут.",
                parse_mode=ParseMode.HTML,
                reply_markup=Keyboards.main_menu()
            )

        @self.dp.message(Command("set"))
        async def cmd_set(message: Message):
            """Команда /set param value — установка параметра напрямую"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            parts = message.text.split(maxsplit=2)
            if len(parts) < 3:
                # Показываем справку
                from src.db.settings import SettingsManager
                with db.get_session() as session:
                    settings = SettingsManager(session)
                    all_settings = settings.get_all()

                lines = ["⚙️ <b>Установка параметров</b>\n",
                         "Формат: /set param value\n",
                         "<b>Доступные параметры:</b>"]
                for name, value in all_settings.items():
                    lines.append(f"<code>{name}</code> = {value}")

                await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
                return

            param_name = parts[1]
            raw_value = parts[2]

            # Определяем тип из DEFAULT_SETTINGS
            from src.db.settings import SettingsManager
            param_info = SettingsManager.DEFAULT_SETTINGS.get(param_name)
            if not param_info:
                await message.answer(f"❌ Неизвестный параметр: {param_name}")
                return

            # Парсим значение
            try:
                if param_info['type'] == 'int':
                    value = int(raw_value)
                elif param_info['type'] == 'float':
                    value = float(raw_value)
                elif param_info['type'] == 'json':
                    import json
                    value = json.loads(raw_value)
                else:
                    value = raw_value
            except (ValueError, Exception) as e:
                await message.answer(f"❌ Ошибка значения: {e}")
                return

            if self.helpers.set_setting_value(param_name, value):
                await message.answer(
                    f"✅ <b>{param_name}</b> = <code>{value}</code>",
                    parse_mode=ParseMode.HTML
                )
            else:
                await message.answer("❌ Ошибка сохранения")

        @self.dp.message(Command("avg_history"))
        async def cmd_avg_history(message: Message):
            """Команда /avg_history — история усреднений"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            from src.db.models import AveragingHistory

            with db.get_session() as session:
                history = session.query(AveragingHistory).order_by(
                    AveragingHistory.created_at.desc()
                ).limit(20).all()

                if not history:
                    await message.answer("📊 <b>Нет истории усреднений</b>", parse_mode=ParseMode.HTML)
                    return

                lines = ["📊 <b>История усреднений</b>\n"]
                for avg in history:
                    lines.append(
                        f"<code>{avg.contract_symbol}</code> #{avg.avg_number}\n"
                        f"  Уровень: {avg.avg_level_pct}% | Цена: ${float(avg.avg_price):.6f}\n"
                        f"  Объем: ${float(avg.avg_amount_usdt):.2f} | Ср. цена: ${float(avg.avg_entry_price):.6f}\n"
                        f"  {avg.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    )

            await message.answer("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())

        @self.dp.message(Command("blacklist"))
        async def cmd_blacklist(message: Message):
            """Команда /blacklist"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_symbol_list(message, "blacklist")

        @self.dp.message(Command("whitelist"))
        async def cmd_whitelist(message: Message):
            """Команда /whitelist"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            await self._show_symbol_list(message, "whitelist")

        # ==================== REPLY KEYBOARD HANDLER ====================
        @self.dp.message(lambda m: m.text and m.from_user and m.from_user.id in self._waiting_list_add)
        async def list_add_text_handler(message: Message):
            """Обработчик ввода символа для добавления в список"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            list_type = self._waiting_list_add.pop(message.from_user.id, None)
            if not list_type:
                return

            raw = message.text.strip().upper()
            # Добавляем _USDT если не указано
            symbol = raw if '_USDT' in raw else f"{raw}_USDT"

            if self.helpers.add_to_symbol_list(symbol, list_type):
                name = "черный" if list_type == "blacklist" else "белый"
                await message.answer(
                    f"✅ <b>{symbol}</b> добавлен в {name} список",
                    parse_mode=ParseMode.HTML,
                    reply_markup=Keyboards.lists_menu(),
                )
            else:
                await message.answer(
                    f"⚠️ <b>{symbol}</b> уже в списке или ошибка",
                    parse_mode=ParseMode.HTML,
                    reply_markup=Keyboards.lists_menu(),
                )

        @self.dp.message(lambda m: m.text in (
            "📊 Статус", "📈 Позиции", "💰 Баланс",
            "⚙️ Настройки", "📉 PnL", "📜 Сделки",
            "🏥 Здоровье", "📋 Контракты", "📊 Стат",
        ))
        async def reply_keyboard_handler(message: Message):
            """Обработчик кнопок reply-клавиатуры"""
            if not config.telegram.is_admin(message.from_user.id):
                return

            text = message.text
            if text == "📊 Статус":
                await self._show_status(message)
            elif text == "📈 Позиции":
                await self._show_positions(message)
            elif text == "💰 Баланс":
                await self._show_balance(message)
            elif text == "⚙️ Настройки":
                await self._show_settings_menu(message)
            elif text == "📉 PnL":
                await self._show_pnl(message)
            elif text == "📜 Сделки":
                await self._show_trades(message)
            elif text == "🏥 Здоровье":
                await self._show_health(message)
            elif text == "📋 Контракты":
                await self._show_contracts(message)
            elif text == "📊 Стат":
                await self._show_stats(message)

        # ==================== CALLBACK HANDLERS ====================
        @self.dp.callback_query(lambda c: c.data)
        async def callback_handler(callback: CallbackQuery):
            """Обработчик всех callback запросов"""
            if not config.telegram.is_admin(callback.from_user.id):
                await callback.answer("❌ Нет доступа")
                return

            try:
                # Отвечаем сразу, чтобы избежать "query is too old" при долгих обработчиках
                await callback.answer()
            except Exception:
                pass

            try:
                action, *args = parse_callback_data(callback.data)

                # Маршрутизация
                if action == "main":
                    await self._cb_main_menu(callback)
                elif action == "status":
                    await self._cb_status(callback)
                elif action == "balance":
                    await self._cb_balance(callback)
                elif action == "positions":
                    await self._cb_positions(callback)
                elif action == "position_detail":
                    await self._cb_position_detail(callback, args[0] if args else "")
                elif action == "position_close":
                    await self._cb_position_close(callback, args[0] if args else "")
                elif action == "position_avg":
                    await self._cb_position_avg(callback, args[0] if args else "")
                elif action == "confirm":
                    await self._cb_confirm(callback, args[0] if args else "", args[1] if len(args) > 1 else "")
                elif action == "cancel":
                    await self._cb_cancel(callback, args[0] if args else "")
                elif action == "pnl":
                    await self._cb_pnl(callback)
                elif action == "settings_menu":
                    await self._cb_settings_menu(callback)
                elif action == "setting_edit":
                    await self._cb_setting_edit(callback, args[0] if args else "")
                elif action == "setting_change":
                    await self._cb_setting_change(callback, args[0] if args else "", args[1] if len(args) > 1 else "")
                elif action == "orderbook_toggle":
                    await self._cb_orderbook_toggle(callback, args[0] if args else "")
                elif action == "stats":
                    await self._cb_stats(callback)
                elif action == "contracts":
                    await self._cb_contracts(callback)
                elif action == "trades":
                    await self._cb_trades(callback, args[0] if args else "all")
                elif action == "health":
                    await self._cb_health(callback)
                elif action == "lists_menu":
                    await self._cb_lists_menu(callback)
                elif action == "blacklist":
                    await self._cb_blacklist(callback)
                elif action == "whitelist":
                    await self._cb_whitelist(callback)
                elif action == "whitelist_toggle":
                    await self._cb_whitelist_toggle(callback)
                elif action == "list_add":
                    await self._cb_list_add(callback, args[0] if args else "")
                elif action == "list_remove":
                    await self._cb_list_remove(callback, args[0] if args else "", args[1] if len(args) > 1 else "")
                elif action == "export_menu":
                    await self._cb_export_menu(callback)
                elif action == "export":
                    await self._cb_export(callback, args[0] if args else "")
                elif action == "notifications":
                    await self._cb_notifications(callback)
                elif action == "notif_toggle":
                    await self._cb_notif_toggle(callback)
                elif action == "stop_trading":
                    await self._cb_stop_trading(callback)
                elif action == "start_trading":
                    await self._cb_start_trading(callback)
                else:
                    await callback.answer(f"❌ Неизвестное действие: {action}")

            except Exception as e:
                err_msg = str(e)
                # "message is not modified" — не ошибка, просто пользователь нажал ту же кнопку
                if "message is not modified" in err_msg:
                    logger.debug(f"Повторное нажатие кнопки (контент не изменился)")
                else:
                    logger.error(f"Ошибка обработки callback: {e}")
                    try:
                        await callback.answer(f"❌ Ошибка: {e}")
                    except Exception:
                        pass  # callback уже просрочен

    # ==================== CALLBACK METHODS ====================

    async def _cb_main_menu(self, callback: CallbackQuery):
        """Главное меню"""
        await callback.message.edit_text(
            "🤖 <b>Gate Futures Bot - Главное меню</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=Keyboards.main_menu()
        )

    async def _cb_status(self, callback: CallbackQuery):
        """Статус бота"""
        await self._show_status(callback)

    async def _cb_balance(self, callback: CallbackQuery):
        """Баланс"""
        await self._show_balance(callback)

    async def _cb_positions(self, callback: CallbackQuery):
        """Позиции"""
        positions = position_manager.get_all_positions()

        if not positions:
            text = "📭 <b>Нет активных позиций</b>"
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())
            return

        lines = ["📊 <b>Активные позиции</b>\n"]

        for symbol, pos in positions.items():
            entry = float(pos.entry_price)
            current = float(pos.current_price) if pos.current_price else entry
            pnl_pct = (entry - current) / entry * 100

            emoji = "📈" if pnl_pct > 0 else "📉"

            lines.append(
                f"\n{emoji} <code>{symbol}</code>\n"
                f"Вход: {self.helpers.format_price(entry)}\n"
                f"Текущая: {self.helpers.format_price(current)}\n"
                f"PnL: {pnl_pct:+.2f}%\n"
                f"Усреднений: {pos.avg_count}"
            )

        # Добавляем кнопки для каждой позиции
        builder = InlineKeyboardBuilder()
        for symbol in positions.keys():
            builder.row(InlineKeyboardButton(text=f"🔧 {symbol}", callback_data=make_callback_data("position_detail", symbol)))
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=make_callback_data("main")))

        await callback.message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())

    async def _cb_position_detail(self, callback: CallbackQuery, symbol: str):
        """Детали позиции"""
        position = position_manager.get_position(symbol)

        if not position:
            await callback.answer("❌ Позиция не найдена")
            return

        entry = float(position.entry_price)
        current = float(position.current_price) if position.current_price else entry
        pnl_pct = (entry - current) / entry * 100
        pnl_usdt = float(position.total_volume_usdt) * pnl_pct / 100

        # Получаем историю усреднений
        avg_data = []
        with db.get_session() as session:
            from src.db.models import AveragingHistory
            avg_history = session.query(AveragingHistory).filter(
                AveragingHistory.contract_symbol == symbol
            ).order_by(AveragingHistory.created_at).all()
            for avg in avg_history:
                avg_data.append({
                    'number': avg.avg_number,
                    'price': float(avg.avg_price),
                    'level_pct': avg.avg_level_pct,
                })

        text = (
            f"📊 <b>Детали позиции</b>\n\n"
            f"<b>Монета:</b> <code>{symbol}</code>\n"
            f"<b>Вход:</b> {self.helpers.format_price(entry)}\n"
            f"<b>Текущая:</b> {self.helpers.format_price(current)}\n"
            f"<b>Объем:</b> ${float(position.total_volume_usdt):.2f}\n"
            f"<b>PnL:</b> {pnl_pct:+.2f}% (${pnl_usdt:+.2f})\n"
            f"<b>Усреднений:</b> {position.avg_count}\n"
            f"<b>Открыта:</b> {position.opened_at.strftime('%H:%M:%S') if position.opened_at else 'N/A'}\n"
        )

        if avg_data:
            text += f"\n<b>История усреднений:</b>\n"
            for avg in avg_data:
                text += f"  #{avg['number']}: {self.helpers.format_price(avg['price'])} ({avg['level_pct']}%)\n"

        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.position_actions(symbol))

    async def _cb_position_close(self, callback: CallbackQuery, symbol: str):
        """Закрытие позиции"""
        text = (
            f"⚠️ <b>Подтвердите закрытие</b>\n\n"
            f"Монета: <code>{symbol}</code>\n"
            f"Позиция будет закрыта по текущей цене."
        )

        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.confirm_action("close_pos", symbol))

    async def _cb_position_avg(self, callback: CallbackQuery, symbol: str):
        """Усреднение позиции"""
        position = position_manager.get_position(symbol)
        if not position:
            await callback.answer("❌ Позиция не найдена")
            return

        # Проверяем можно ли усреднить
        signal = position_manager.should_add_averaging(symbol, float(position.current_price or position.entry_price))

        if signal:
            avg_number, avg_level = signal
            # Добавляем усреднение
            avg_volume = self.helpers.get_setting_value('initial_position_usdt') or 10.0
            success = await position_manager.add_averaging(
                symbol,
                float(position.current_price or position.entry_price),
                avg_volume,
                avg_number,
                avg_level,
            )

            if success:
                await callback.answer(f"✅ Усреднение #{avg_number} добавлено")
                await self._cb_position_detail(callback, symbol)
            else:
                await callback.answer("❌ Ошибка усреднения")
        else:
            await callback.answer("ℹ️ Нет условия для усреднения")

    async def _cb_confirm(self, callback: CallbackQuery, action: str, symbol: str):
        """Подтверждение действия"""
        if action == "close_pos":
            success, message = await self.helpers.close_position_manual(symbol)
            if success:
                await callback.answer(f"✅ {message}")
                await self._cb_positions(callback)
            else:
                await callback.answer(f"❌ {message}")

    async def _cb_cancel(self, callback: CallbackQuery, symbol: str):
        """Отмена действия"""
        if symbol:
            await self._cb_position_detail(callback, symbol)
        else:
            await self._cb_main_menu(callback)

    async def _cb_pnl(self, callback: CallbackQuery):
        """PnL"""
        await self._show_pnl(callback)

    async def _cb_settings_menu(self, callback: CallbackQuery):
        """Меню настроек"""
        await self._show_settings_menu(callback)

    async def _cb_setting_edit(self, callback: CallbackQuery, param: str):
        """Редактирование настройки"""
        param_names = {
            'position_size': ('Объем позиции USDT', 'initial_position_usdt', config.trading.default_position_size_usdt),
            'tp_pct': ('Take Profit %', 'take_profit_pct', config.trading.take_profit_percentage),
            'ath_ratio': ('ATH ratio', 'ath_ratio_threshold', config.trading.ath_ratio_threshold),
            'days': ('Дней с листинга', 'days_since_listing_limit', config.trading.days_since_listing_limit),
            'max_coins': ('Максимум монет', 'max_concurrent_coins', 10),
            'drawdown': ('Макс. просадка %', 'max_drawdown_pct', 50),
            'avg_levels': ('Усреднения', 'avg_levels', [300, 700, 1000]),
            'max_avg_count': ('Макс. усреднений', 'max_avg_count', 3),
            'avg_amount': ('Объем усреднения USDT', 'avg_amount_usdt', 10),
            'protection': ('Защита баланса', None, None),
            'acceleration': ('Режим разгона', None, None),
            'orderbook': ('Мониторинг стакана', None, None),
        }

        if param not in param_names:
            await callback.answer("❌ Неизвестный параметр")
            return

        name, setting_key, default = param_names[param]
        current_value = self.helpers.get_setting_value(setting_key) or default

        if param == "avg_levels":
            text = f"📊 <b>{name}</b>\n\nТекущее: {current_value}\n\nИзмените через /set"
        elif param == "protection":
            trigger = self.helpers.get_setting_value('protection_trigger_pct') or 50
            transfer = self.helpers.get_setting_value('protection_transfer_pct') or 25
            text = f"💳 <b>Защита баланса</b>\n\nТриггер: {trigger}%\nПеревод: {transfer}%\n\nИзмените через /set"
        elif param == "acceleration":
            enabled = self.helpers.get_setting_value('acceleration_enabled') or False
            step = self.helpers.get_setting_value('acceleration_step_pct') or 10
            max_mult = self.helpers.get_setting_value('acceleration_max_multiplier') or 3.0
            from src.risk.acceleration import acceleration_manager
            multipliers = acceleration_manager.get_all_multipliers()
            mult_text = ""
            if multipliers:
                mult_text = "\n\n<b>Текущие множители:</b>\n"
                for s, m in multipliers.items():
                    mult_text += f"  {s}: x{m:.2f}\n"
            text = (
                f"🚀 <b>Режим разгона</b>\n\n"
                f"Статус: {'Вкл' if enabled else 'Выкл'}\n"
                f"Шаг: {step}% за TP\n"
                f"Макс. множитель: x{max_mult:.1f}\n"
                f"{mult_text}\n"
                f"Для изменения: /set acceleration_enabled true\n"
                f"/set acceleration_step_pct 15\n"
                f"/set acceleration_max_multiplier 2.5"
            )
        elif param == "orderbook":
            ob_enabled = self.helpers.get_setting_value('orderbook_monitoring_enabled')
            if ob_enabled is None:
                ob_enabled = False
            throttle = self.helpers.get_setting_value('orderbook_update_throttle_ms') or 500
            check_before = self.helpers.get_setting_value('check_orderbook_before_entry')
            if check_before is None:
                check_before = True
            text = (
                f"📡 <b>Мониторинг стакана</b>\n\n"
                f"Статус: {'✅ Вкл' if ob_enabled else '❌ Выкл'}\n"
                f"Проверка перед входом: {'✅ Да' if check_before else '❌ Нет'}\n"
                f"Throttle: {throttle} мс\n\n"
                f"Для throttle: /set orderbook_update_throttle_ms 300"
            )
            # Кнопки toggle вместо +/-
            builder = InlineKeyboardBuilder()
            toggle_text = "❌ Выключить" if ob_enabled else "✅ Включить"
            builder.row(InlineKeyboardButton(text=toggle_text, callback_data=make_callback_data("orderbook_toggle", "monitoring")))
            check_toggle_text = "❌ Не проверять перед входом" if check_before else "✅ Проверять перед входом"
            builder.row(InlineKeyboardButton(text=check_toggle_text, callback_data=make_callback_data("orderbook_toggle", "check_entry")))
            builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=make_callback_data("settings_menu")))
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())
            return
        else:
            text = f"⚙️ <b>{name}</b>\n\nТекущее значение: {current_value}\n\nИспользуйте +/- для изменения"

        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.setting_edit(param, str(current_value)))

    async def _cb_setting_change(self, callback: CallbackQuery, param: str, delta: str):
        """Изменение настройки"""
        param_map = {
            'position_size': ('initial_position_usdt', config.trading.default_position_size_usdt, 0),
            'tp_pct': ('take_profit_pct', config.trading.take_profit_percentage, 1),
            'ath_ratio': ('ath_ratio_threshold', config.trading.ath_ratio_threshold, 2),
            'days': ('days_since_listing_limit', config.trading.days_since_listing_limit, 0),
            'max_coins': ('max_concurrent_coins', 10, 0),
            'drawdown': ('max_drawdown_pct', 50, 0),
            'max_avg_count': ('max_avg_count', 3, 0),
            'avg_amount': ('avg_amount_usdt', 10, 0),
        }

        if param not in param_map:
            await callback.answer("❌ Неизвестный параметр")
            return

        setting_key, default, precision = param_map[param]
        current = self.helpers.get_setting_value(setting_key) or default

        # Вычисляем новое значение с использованием Decimal для точности
        try:
            new_value = float(Decimal(str(current)) + Decimal(delta))
            if precision == 0:
                new_value = int(round(new_value))
            else:
                new_value = round(new_value, precision)
        except (ValueError, Exception):
            await callback.answer("❌ Ошибка значения")
            return

        # Валидация
        if new_value < 0:
            new_value = 0
        if param == 'tp_pct' and new_value > 50:
            new_value = 50
        if param == 'ath_ratio' and new_value > 1:
            new_value = 1
        if param == 'days' and new_value > 365:
            new_value = 365

        # Сохраняем
        if self.helpers.set_setting_value(setting_key, new_value):
            await callback.answer(f"✅ Значение изменено на {new_value}")
            await self._cb_setting_edit(callback, param)
        else:
            await callback.answer("❌ Ошибка сохранения")

    async def _cb_orderbook_toggle(self, callback: CallbackQuery, toggle_type: str):
        """Переключение настроек стакана"""
        if toggle_type == "monitoring":
            current = self.helpers.get_setting_value('orderbook_monitoring_enabled')
            new_val = not bool(current)
            self.helpers.set_setting_value('orderbook_monitoring_enabled', new_val)
            await callback.answer(f"📡 Мониторинг стакана: {'Вкл' if new_val else 'Выкл'}")
        elif toggle_type == "check_entry":
            current = self.helpers.get_setting_value('check_orderbook_before_entry')
            if current is None:
                current = True
            new_val = not bool(current)
            self.helpers.set_setting_value('check_orderbook_before_entry', new_val)
            await callback.answer(f"📡 Проверка стакана перед входом: {'Вкл' if new_val else 'Выкл'}")
        else:
            await callback.answer("❌ Неизвестный параметр")
            return
        # Обновляем отображение
        await self._cb_setting_edit(callback, "orderbook")

    async def _cb_stats(self, callback: CallbackQuery):
        """Статистика"""
        await self._show_stats(callback)

    async def _cb_contracts(self, callback: CallbackQuery):
        """Контракты"""
        await self._show_contracts(callback)

    async def _cb_trades(self, callback: CallbackQuery, filter_type: str):
        """Сделки"""
        await self._show_trades(callback, filter_type)

    async def _cb_health(self, callback: CallbackQuery):
        """Здоровье системы"""
        await self._show_health(callback)

    def _get_whitelist_only(self) -> bool:
        """Получить текущее значение whitelist_only"""
        try:
            from src.db.settings import SettingsManager
            with db.get_session() as session:
                settings = SettingsManager(session)
                return settings.get('whitelist_only', False)
        except Exception:
            return False

    async def _cb_lists_menu(self, callback: CallbackQuery):
        """Меню списков"""
        blacklist_count = len(self.helpers.get_symbol_list('blacklist'))
        whitelist_count = len(self.helpers.get_symbol_list('whitelist'))
        wl_only = self._get_whitelist_only()

        mode_text = "🟢 Режим: только белый список" if wl_only else "⚪ Режим: все новые листинги"
        text = (
            "🚫/✅ <b>Списки монет</b>\n\n"
            f"{mode_text}\n\n"
            f"🚫 Черный список: {blacklist_count} монет\n"
            f"✅ Белый список: {whitelist_count} монет\n\n"
            "Выберите действие:"
        )

        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.lists_menu(wl_only))

    async def _cb_whitelist_toggle(self, callback: CallbackQuery):
        """Переключить режим whitelist-only"""
        from src.db.settings import SettingsManager
        with db.get_session() as session:
            settings = SettingsManager(session)
            current = settings.get('whitelist_only', False)
            settings.set('whitelist_only', not current, updated_by='telegram')
        # Обновляем меню
        await self._cb_lists_menu(callback)

    async def _cb_blacklist(self, callback: CallbackQuery):
        """Черный список"""
        await self._show_symbol_list(callback, "blacklist")

    async def _cb_whitelist(self, callback: CallbackQuery):
        """Белый список"""
        await self._show_symbol_list(callback, "whitelist")

    async def _cb_list_add(self, callback: CallbackQuery, list_type: str):
        """Добавление в список"""
        self._waiting_list_add[callback.from_user.id] = list_type
        name = "черный" if list_type == "blacklist" else "белый"
        await callback.message.edit_text(
            f"➕ <b>Добавить в {name} список</b>\n\n"
            f"Отправьте символ монеты (например: BTC_USDT или BTC)",
            parse_mode=ParseMode.HTML
        )

    async def _cb_list_remove(self, callback: CallbackQuery, list_type: str, symbol: str):
        """Удаление из списка"""
        if self.helpers.remove_from_symbol_list(symbol, list_type):
            await callback.answer(f"✅ {symbol} удален из списка")
            if list_type == "blacklist":
                await self._cb_blacklist(callback)
            else:
                await self._cb_whitelist(callback)
        else:
            await callback.answer("❌ Ошибка удаления")

    async def _cb_export_menu(self, callback: CallbackQuery):
        """Меню экспорта"""
        text = (
            "📥 <b>Экспорт данных</b>\n\n"
            "Выберите тип экспорта:"
        )

        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.export_menu())

    async def _cb_export(self, callback: CallbackQuery, export_type: str):
        """Экспорт данных"""
        if export_type == "trades":
            data = self.helpers.export_trades_csv()
            if data:
                await callback.message.answer_document(
                    types.BufferedInputFile(
                        data,
                        filename=f"trades_{_msk_now().strftime('%Y%m%d_%H%M%S')}.csv"
                    )
                )
                await callback.answer("✅ Сделки экспортированы")
            else:
                await callback.answer("❌ Ошибка экспорта")
        elif export_type == "positions":
            data = self.helpers.export_positions_csv()
            if data:
                await callback.message.answer_document(
                    types.BufferedInputFile(
                        data,
                        filename=f"positions_{_msk_now().strftime('%Y%m%d_%H%M%S')}.csv"
                    )
                )
                await callback.answer("✅ Позиции экспортированы")
            else:
                await callback.answer("❌ Ошибка экспорта")
        elif export_type == "stats":
            # Текстовая статистика
            with db.get_session() as session:
                total_trades = session.query(Trade).count()
                total_positions = session.query(Position).count()
                active = session.query(Position).filter(Position.status == 'open').count()

            text = (
                f"📊 <b>Статистика</b>\n\n"
                f"Всего сделок: {total_trades}\n"
                f"Всего позиций: {total_positions}\n"
                f"Активных: {active}\n"
            )
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.export_menu())
        elif export_type == "settings":
            from src.db.settings import SettingsManager
            with db.get_session() as session:
                settings = SettingsManager(session)
                all_settings = settings.get_all()

            lines = ["📋 <b>Настройки</b>\n"]
            for name, value in all_settings.items():
                lines.append(f"{name}: {value}")

            await callback.message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=Keyboards.export_menu())

    async def _cb_notifications(self, callback: CallbackQuery):
        """Уведомления"""
        await self._show_notifications(callback)

    async def _cb_notif_toggle(self, callback: CallbackQuery):
        """Переключение уведомлений"""
        if self.notifier._enabled:
            self.notifier.disable()
            await callback.answer("🔕 Уведомления выключены")
        else:
            self.notifier.enable()
            await callback.answer("🔔 Уведомления включены")

        await self._show_notifications(callback)

    async def _cb_stop_trading(self, callback: CallbackQuery):
        """Остановка торговли"""
        self.helpers.set_setting_value('max_concurrent_coins', 0)

        text = (
            "🛑 <b>Торговля остановлена</b>\n\n"
            "Новые позиции открываться не будут.\n"
            "Текущие позиции продолжат работать.\n\n"
            "Для возобновления используйте кнопку ▶️ Запуск"
        )

        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())
        await callback.answer("✅ Торговля остановлена")

    async def _cb_start_trading(self, callback: CallbackQuery):
        """Запуск торговли"""
        current = self.helpers.get_setting_value('max_concurrent_coins') or 0
        if current == 0:
            self.helpers.set_setting_value('max_concurrent_coins', 10)

        text = (
            "▶️ <b>Торговля запущена</b>\n\n"
            "Бот будет открывать новые позиции."
        )

        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())
        await callback.answer("✅ Торговля запущена")

    # ==================== MESSAGE METHODS ====================

    async def _show_status(self, message):
        """Показать статус бота"""
        risk_status = risk_manager.get_status()
        health = self.helpers.get_system_health()

        status_text = (
            "🔧 <b>Статус бота</b>\n\n"
            f"<b>Бот:</b> {'✅ Работает' if health.get('bot_running') else '❌ Остановлен'}\n"
            f"<b>WebSocket:</b> {'✅ Подключен' if health.get('ws_connected') else '❌ Отключен'}\n"
            f"<b>База данных:</b> {'✅ OK' if health.get('db_ok') else '❌ Ошибка'}\n"
            f"<b>API:</b> {'✅ OK' if health.get('api_ok') else '❌ Ошибка'}\n\n"
            f"<b>Баланс:</b> ${risk_status['balance'] or 0:.2f}\n"
            f"<b>PnL за день:</b> ${risk_status['daily_pnl']:+.2f}\n\n"
            f"<b>Предохранитель:</b> {risk_status['circuit_breaker_state']}\n"
            f"<b>Активных позиций:</b> {len(position_manager.get_all_positions())}"
        )

        if isinstance(message, CallbackQuery):
            await message.message.edit_text(status_text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())
        else:
            await message.answer(status_text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())

    async def _show_pnl(self, message):
        """Показать PnL"""
        pnl = risk_manager.get_daily_pnl()
        emoji = "📈" if pnl > 0 else "📉" if pnl < 0 else "➡️"

        text = (
            f"{emoji} <b>PnL за день</b>\n\n"
            f"Прибыль/убыток: ${pnl:+.2f}\n"
            f"Дата: {_msk_now().strftime('%Y-%m-%d')}"
        )

        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())

    async def _show_settings_menu(self, message):
        """Показать меню настроек"""
        position_size = self.helpers.get_setting_value('initial_position_usdt') or 10
        tp_pct = self.helpers.get_setting_value('take_profit_pct') or 2.0
        ath_ratio = self.helpers.get_setting_value('ath_ratio_threshold') or 0.3
        days = self.helpers.get_setting_value('days_since_listing_limit') or 30
        max_coins = self.helpers.get_setting_value('max_concurrent_coins') or 10
        drawdown = self.helpers.get_setting_value('max_drawdown_pct') or 50
        max_avg = self.helpers.get_setting_value('max_avg_count') or 3

        accel_enabled = self.helpers.get_setting_value('acceleration_enabled') or False
        accel_step = self.helpers.get_setting_value('acceleration_step_pct') or 10
        accel_max = self.helpers.get_setting_value('acceleration_max_multiplier') or 3.0

        ob_enabled = self.helpers.get_setting_value('orderbook_monitoring_enabled') or False

        text = (
            "⚙️ <b>Настройки</b>\n\n"
            f"💵 Объем позиции: ${position_size:.0f}\n"
            f"💵 Объем усреднения: ${position_size:.0f} (= позиция)\n"
            f"🎯 TP %: {tp_pct:.1f}%\n"
            f"📊 ATH ratio: {ath_ratio:.2f}\n"
            f"📅 Дней листинг: {days}\n"
            f"🔢 Максимум монет: {max_coins}\n"
            f"🔢 Макс. усреднений: {max_avg}\n"
            f"📉 Просадка: {drawdown}%\n"
            f"\n<b>Разгон:</b> {'Вкл' if accel_enabled else 'Выкл'}"
            f" (шаг {accel_step}%, макс x{accel_max:.1f})\n"
            f"<b>Стакан:</b> {'Вкл' if ob_enabled else 'Выкл'}\n"
        )

        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.settings_menu())
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.settings_menu())

    async def _show_positions(self, message):
        """Показать позиции"""
        positions = position_manager.get_all_positions()

        if not positions:
            text = "📭 Нет активных позиций"
            if isinstance(message, CallbackQuery):
                await message.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())
            else:
                await message.answer(text, reply_markup=Keyboards.main_menu())
            return

        lines = ["📊 <b>Активные позиции</b>"]

        for symbol, pos in positions.items():
            entry = float(pos.entry_price)
            current = float(pos.current_price) if pos.current_price else entry
            pnl_pct = (entry - current) / entry * 100

            emoji = "📈" if pnl_pct > 0 else "📉"

            lines.append(
                f"\n{emoji} <code>{symbol}</code>\n"
                f"Вход: {self.helpers.format_price(entry)}\n"
                f"Текущая: {self.helpers.format_price(current)}\n"
                f"PnL: {pnl_pct:+.2f}%\n"
                f"Усреднений: {pos.avg_count}"
            )

        text = "\n".join(lines)
        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())

    async def _show_balance(self, message):
        """Показать баланс"""
        futures_balance = await risk_manager.balance_checker.get_balance()

        if futures_balance is None:
            text = (
                "💰 <b>Балансы</b>\n\n"
                "⚠️ Не удалось получить баланс\n\n"
                f"DRY_RUN режим: {config.dry_run}"
            )
        else:
            text = f"💰 <b>Балансы</b>\n\n<b>Фьючерсы:</b> ${futures_balance:.2f} USDT"

        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())

    async def _show_settings(self, message: Message):
        """Показать настройки"""
        from src.db.settings import SettingsManager
        with db.get_session() as session:
            settings = SettingsManager(session)
            all_settings = settings.get_all()

        lines = ["📋 <b>Текущие настройки</b>\n"]
        for name, value in all_settings.items():
            lines.append(f"<b>{name}:</b> <code>{value}</code>")

        lines.append("\nДля изменения используйте кнопки ниже или /set")

        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=Keyboards.settings_menu())

    async def _show_stats(self, message: Message):
        """Показать статистику"""
        with db.get_session() as session:
            today = datetime.utcnow().date()
            start_of_day = datetime.combine(today, datetime.min.time())

            trades = session.query(Trade).filter(
                Trade.created_at >= start_of_day
            ).all()

            total_trades = len(trades)
            total_pnl = sum(t.pnl or 0 for t in trades)
            winning = sum(1 for t in trades if (t.pnl or 0) > 0)
            losing = sum(1 for t in trades if (t.pnl or 0) < 0)

            win_rate = winning / total_trades * 100 if total_trades > 0 else 0

            total_positions = session.query(Position).count()
            active_positions = session.query(Position).filter(
                Position.status == 'open'
            ).count()

        text = (
            f"📊 <b>Статистика</b>\n\n"
            f"<b>Сегодня ({today.strftime('%Y-%m-%d')}):</b>\n"
            f"Сделок: {total_trades}\n"
            f"Прибыльных: {winning}\n"
            f"Убыточных: {losing}\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"PnL: ${total_pnl:+.2f}\n\n"
            f"<b>Позиции:</b>\n"
            f"Всего: {total_positions}\n"
            f"Активных: {active_positions}"
        )

        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())

    async def _show_contracts(self, message: Message):
        """Показать контракты"""
        from src.db.models import Contract
        from datetime import datetime

        text = "📋 <b>Контракты</b>\n\n"

        with db.get_session() as session:
            in_work = session.query(Contract).filter(
                Contract.listing_taken_in_work == True,
                Contract.status != 'completed'
            ).all()

            completed = session.query(Contract).filter(
                Contract.status == 'completed'
            ).order_by(Contract.updated_at.desc()).limit(5).all()

            if in_work:
                text += "<b>В работе:</b>\n"
                for contract in in_work[:10]:
                    days = (datetime.utcnow() - contract.launch_time).days if contract.launch_time else 0
                    text += f"• {contract.symbol} (листинг: {days}дн. назад)\n"
                if len(in_work) > 10:
                    text += f"... и еще {len(in_work) - 10}\n"
            else:
                text += "В работе: 0\n"

            if completed:
                text += f"\n<b>Завершены (последние 5):</b>\n"
                for contract in completed:
                    text += f"• {contract.symbol}\n"

        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())
            await message.answer()
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())

    async def _show_trades(self, message: Message, filter_type: str = "all"):
        """Показать сделки"""
        with db.get_session() as session:
            query = session.query(Trade)

            if filter_type == "today":
                start = datetime.combine(date.today(), datetime.min.time())
                query = query.filter(Trade.created_at >= start)
            elif filter_type == "week":
                start = datetime.utcnow() - timedelta(days=7)
                query = query.filter(Trade.created_at >= start)
            elif filter_type == "profit":
                query = query.filter(Trade.pnl > 0)
            elif filter_type == "loss":
                query = query.filter(Trade.pnl < 0)

            trades = query.order_by(Trade.created_at.desc()).limit(20).all()

            if not trades:
                text = "📜 <b>Нет сделок</b>"
            else:
                lines = ["📜 <b>Последние сделки</b>\n"]

                for trade in trades:
                    type_emoji = {
                        'open': '🟢',
                        'close': '🔴',
                        'tp_close': '✅',
                        'avg_open': '📊',
                        'reopen': '🔄',
                    }.get(trade.trade_type, '•')

                    pnl_str = f"${float(trade.pnl):+.2f}" if trade.pnl else "N/A"
                    lines.append(
                        f"{type_emoji} <code>{trade.contract_symbol}</code>\n"
                        f"{trade.trade_type} | {self.helpers.format_price(float(trade.price))} | "
                        f"PnL: {pnl_str}\n"
                        f"{trade.created_at.strftime('%H:%M:%S')}\n"
                    )

                text = "\n".join(lines)

        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.trades_filter())
            await message.answer()
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.trades_filter())

    async def _show_health(self, message: Message):
        """Показать здоровье системы"""
        health = self.helpers.get_system_health()

        text = (
            "🏥 <b>Здоровье системы</b>\n\n"
            f"🤖 Бот: {'✅ Работает' if health.get('bot_running') else '❌ Остановлен'}\n"
            f"🔌 WebSocket: {'✅ Подключен' if health.get('ws_connected') else '❌ Отключен'}\n"
            f"💾 База данных: {'✅ OK' if health.get('db_ok') else '❌ Ошибка'}\n"
            f"🌐 API: {'✅ OK' if health.get('api_ok') else '❌ Ошибка'}\n"
        )

        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())
            await message.answer()
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.main_menu())

    async def _show_symbol_list(self, message: Message, list_type: str):
        """Показать список символов с кнопками удаления"""
        items = self.helpers.get_symbol_list(list_type)

        emoji = "🚫" if list_type == "blacklist" else "✅"
        name = "Черный список" if list_type == "blacklist" else "Белый список"

        if not items:
            text = f"{emoji} <b>{name}</b>\n\nСписок пуст"
        else:
            lines = [f"{emoji} <b>{name}</b>\n"]
            for item in items:
                lines.append(f"• {item['symbol']}")
            text = "\n".join(lines)

        # Строим клавиатуру с кнопками удаления
        builder = InlineKeyboardBuilder()
        for item in items:
            sym = item['symbol']
            builder.row(InlineKeyboardButton(
                text=f"❌ {sym}",
                callback_data=make_callback_data("list_remove", list_type, sym),
            ))
        add_label = "➕ Добавить"
        builder.row(InlineKeyboardButton(text=add_label, callback_data=make_callback_data("list_add", list_type)))
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=make_callback_data("lists_menu")))
        kb = builder.as_markup()

        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            await message.answer()
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def _show_notifications(self, message: Message):
        """Показать настройки уведомлений"""
        status = "включены" if self.notifier._enabled else "выключены"
        emoji = "🔔" if self.notifier._enabled else "🔕"

        text = (
            f"{emoji} <b>Уведомления</b>\n\n"
            f"Статус: {status}"
        )

        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.notifications_toggle(self.notifier._enabled))
            await message.answer()
        else:
            await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=Keyboards.notifications_toggle(self.notifier._enabled))

    # ==================== START/STOP ====================

    async def start(self):
        """Запустить бота"""
        if self._running:
            logger.warning("Telegram бот уже запущен")
            return

        # Удаляем webhook для избежания конфликтов
        try:
            await self.bot.delete_webhook(drop_pending_updates=True)
            logger.info("Webhook удален, pending updates сброшены")
        except Exception as e:
            logger.warning(f"Не удалось удалить webhook: {e}")

        self._running = True
        logger.info("Telegram бот запущен")

        # Отправляем уведомление о запуске с главным меню
        for admin_id in config.telegram.admin_ids:
            try:
                await self.bot.send_message(
                    admin_id,
                    "🚀 <b>Бот запущен</b>\n\nГотов к работе!",
                    parse_mode=ParseMode.HTML,
                    reply_markup=Keyboards.main_menu()
                )
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения о запуске админу {admin_id}: {e}")

    async def stop(self):
        """Остановить бота"""
        if not self._running:
            return

        self._running = False

        # Отправляем уведомление об остановке
        for admin_id in config.telegram.admin_ids:
            try:
                await self.bot.send_message(
                    admin_id,
                    "🛑 <b>Бот остановлен</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения об остановке админу {admin_id}: {e}")

        logger.info("Telegram бот остановлен")

    async def run(self):
        """Запустить polling"""
        await self.start()
        try:
            await self.dp.start_polling(
                self.bot,
                drop_pending_updates=True
            )
        finally:
            await self.stop()


# ========================= GLOBAL INSTANCES =========================
_telegram_bot_instance: Optional[TelegramBot] = None


def get_telegram_bot() -> TelegramBot:
    """Получить инстанс Telegram бота"""
    global _telegram_bot_instance
    if _telegram_bot_instance is None:
        _telegram_bot_instance = TelegramBot()
    return _telegram_bot_instance


def get_notifier() -> TelegramNotifier:
    """Получить notifier"""
    return get_telegram_bot().notifier


# Для удобства использования
class TelegramBotAccessor:
    """Доступ к боту как к свойству"""

    def __get__(self, obj, objtype=None):
        return get_telegram_bot()

    def __getattr__(self, name):
        """Делегирование всех методов к реальному боту"""
        return getattr(get_telegram_bot(), name)


class NotifierAccessor:
    """Доступ к notifier как к свойству"""

    def __get__(self, obj, objtype=None):
        return get_notifier()


telegram_bot = TelegramBotAccessor()
notifier = NotifierAccessor()
