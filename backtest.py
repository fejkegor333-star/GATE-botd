"""
Backtest strategy: SHORT on new Gate.io futures listings.
Uses public Gate.io API for historical candlestick data.

Модель максимально приближена к реальной логике бота:
  - Вход: Market IOC с проскальзыванием и вероятностью неисполнения
  - TP: Лимитный IOC — исполняется только если low свечи <= TP цена
  - SL: По умолчанию ВЫКЛ (как в боте), только таймаут
  - Усреднение: IOC с вероятностью неисполнения
  - Закрытие по таймауту: маркетом (по close свечи)

Usage:
  python backtest.py                     # Default params (как бот)
  python backtest.py --sl 10             # С опциональным стоп-лоссом
  python backtest.py --delay 30          # 30 min entry delay
  python backtest.py --months 3          # Last 3 months
  python backtest.py --no-reopen         # Disable reopen after TP
  python backtest.py --no-avg            # Disable averaging
  python backtest.py --slippage 0        # Без проскальзывания (идеальный)
  python backtest.py --fill-rate 1.0     # 100% исполнение (идеальный)
"""
import asyncio
import aiohttp
import argparse
import json
import math
import random
import time
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict

# ============================================================
# STABLECOINS & NON-CRYPTO FILTERS
# ============================================================
STABLECOINS = {
    'USDC', 'BUSD', 'DAI', 'TUSD', 'FDUSD', 'USDD', 'PYUSD', 'USDP',
    'GUSD', 'FRAX', 'LUSD', 'CEUR', 'SUSD', 'MIM', 'UST', 'USTC',
    'EURC', 'EURT', 'USDJ', 'CUSD', 'USDN', 'USDK', 'HUSD', 'TRIBE',
    'USD1', 'USDY', 'USDX', 'USDE', 'RLUSD', 'GHO', 'CRVUSD',
}
NON_CRYPTO_TYPES = {'stocks', 'indices', 'metals', 'commodities', 'forex'}

KNOWN_NON_CRYPTO = {
    'GEELY', 'KUAISHOU', 'ZHIPU', 'XUNCE', 'XIAOMI', 'BABA', 'KWEB',
    'SPACEX', 'TSLAX', 'MSTRX', 'NVDAX', 'METAX', 'GOOGLX', 'AAPLX',
    'GER40', 'US30', 'US100', 'HK50', 'JP225', 'UK100', 'VIX', 'GVZ',
    'XAU', 'XAG', 'XPT', 'XPD', 'XCU', 'PAXG', 'IAU', 'SLVON',
}


def is_filtered(symbol: str, contract_data: dict) -> bool:
    ct = contract_data.get('contract_type', '')
    if ct in NON_CRYPTO_TYPES:
        return True
    if contract_data.get('is_pre_market') is True:
        return True
    base = symbol.split('_')[0].upper()
    if base in STABLECOINS or base in KNOWN_NON_CRYPTO:
        return True
    return False


# ============================================================
# MODELS
# ============================================================
@dataclass
class Position:
    symbol: str
    entry_price: float
    initial_entry_price: float
    volume_usdt: float
    avg_count: int = 0
    opened_at: datetime = field(default_factory=datetime.utcnow)
    max_adverse_pct: float = 0.0


@dataclass
class TradeResult:
    symbol: str
    entry: float
    exit_price: float
    pnl: float
    pnl_pct: float
    reason: str
    hold_hours: float
    avg_count: int
    volume: float
    max_adverse: float
    opened_at: datetime = field(default_factory=datetime.utcnow)


# ============================================================
# GATE.IO API
# ============================================================
API_BASE = 'https://api.gateio.ws/api/v4'
_request_count = 0


async def api_get(session: aiohttp.ClientSession, url: str, params: dict = None) -> list:
    global _request_count
    _request_count += 1
    # Rate limit: max ~8 req/s
    if _request_count % 8 == 0:
        await asyncio.sleep(1.0)
    try:
        async with session.get(url, params=params) as resp:
            if resp.status == 429:
                await asyncio.sleep(3)
                return await api_get(session, url, params)
            if resp.status != 200:
                return []
            return await resp.json()
    except Exception:
        return []


async def fetch_contracts(session) -> list:
    return await api_get(session, f'{API_BASE}/futures/usdt/contracts')


async def fetch_candles(session, contract: str, from_ts: int, to_ts: int,
                        interval: str = '5m') -> list:
    """Fetch all candles in batches. Gate.io does not allow limit+from+to together."""
    all_candles = []
    current = from_ts
    while current < to_ts:
        batch_end = min(current + 1999 * 300, to_ts)
        data = await api_get(session, f'{API_BASE}/futures/usdt/candlesticks', {
            'contract': contract,
            'interval': interval,
            'from': current,
            'to': batch_end,
        })
        if not data or not isinstance(data, list):
            break

        all_candles.extend(data)

        last_t = int(data[-1].get('t', 0))
        if last_t <= current:
            break
        current = last_t + 1

    return all_candles


def parse_candles(raw: list) -> list:
    """Parse raw candle dicts to tuples (t, o, h, l, c, vol)."""
    result = []
    for c in raw:
        try:
            t = int(c.get('t', 0))
            o = float(c.get('o', 0))
            h = float(c.get('h', 0))
            l = float(c.get('l', 0))
            cl = float(c.get('c', 0))
            v = float(c.get('v', 0) or 0)
            if h > 0 and l > 0 and t > 0:
                result.append((t, o, h, l, cl, v))
        except (ValueError, TypeError):
            continue
    result.sort(key=lambda x: x[0])
    return result


# ============================================================
# BACKTEST ENGINE
# ============================================================
class Backtester:
    def __init__(self, args):
        self.args = args
        self.positions: Dict[str, Position] = {}
        self.trades: List[TradeResult] = []
        self.balance = args.balance
        self.initial_balance = args.balance
        self.peak_balance = args.balance
        self.max_drawdown = 0.0
        self.total_fees = 0.0
        self.total_volume = 0.0
        self.symbols_traded = set()
        self.daily_pnl: Dict[str, float] = {}
        self.reopen_count = 0
        # Статистика реалистичной модели
        self.entry_failures = 0
        self.avg_failures = 0
        self.tp_rejections = 0

    def _open(self) -> int:
        return len(self.positions)

    def open_position(self, symbol: str, price: float, ts: datetime):
        if symbol in self.positions:
            return False
        if self._open() >= self.args.max_positions:
            return False

        # Моделирование IOC: вероятность неисполнения
        if random.random() > self.args.fill_rate:
            self.entry_failures += 1
            return False

        # Проскальзывание на входе (для SHORT: цена исполнения выше = хуже)
        if self.args.slippage > 0:
            slip = random.uniform(0, self.args.slippage)
            price = price * (1 + slip / 100)

        vol = self.args.position
        margin_needed = vol * 0.15
        if self.balance < margin_needed:
            return False

        fee = vol * self.args.fee
        self.total_fees += fee
        self.balance -= fee
        self.total_volume += vol
        self.symbols_traded.add(symbol)

        self.positions[symbol] = Position(
            symbol=symbol,
            entry_price=price,
            initial_entry_price=price,
            volume_usdt=vol,
            opened_at=ts,
        )
        return True

    def close_position(self, symbol: str, price: float, reason: str,
                       ts: datetime) -> Optional[TradeResult]:
        pos = self.positions.get(symbol)
        if not pos:
            return None

        fee = pos.volume_usdt * self.args.fee
        self.total_fees += fee
        self.balance -= fee

        pnl_pct = (pos.entry_price - price) / pos.entry_price * 100
        pnl_usdt = pos.volume_usdt * (pos.entry_price - price) / pos.entry_price

        self.balance += pnl_usdt

        if self.balance > self.peak_balance:
            self.peak_balance = self.balance
        dd = self.peak_balance - self.balance
        if dd > self.max_drawdown:
            self.max_drawdown = dd

        hold = (ts - pos.opened_at).total_seconds() / 3600
        day_key = ts.strftime('%Y-%m-%d')
        self.daily_pnl[day_key] = self.daily_pnl.get(day_key, 0) + pnl_usdt

        trade = TradeResult(
            symbol=symbol, entry=pos.entry_price, exit_price=price,
            pnl=pnl_usdt, pnl_pct=pnl_pct, reason=reason,
            hold_hours=hold, avg_count=pos.avg_count,
            volume=pos.volume_usdt, max_adverse=pos.max_adverse_pct,
            opened_at=pos.opened_at,
        )
        self.trades.append(trade)
        del self.positions[symbol]
        return trade

    def process_candle(self, symbol: str, h: float, l: float, c: float, ts: datetime):
        """Process one candle for an open position. Returns close reason or None."""
        pos = self.positions.get(symbol)
        if not pos:
            return None

        # Track max adverse move (price rise for SHORT)
        adverse = (h - pos.entry_price) / pos.entry_price * 100
        if adverse > pos.max_adverse_pct:
            pos.max_adverse_pct = adverse

        # Check averaging (price rise triggers averaging for SHORT)
        if self.args.max_avg > 0 and pos.avg_count < self.args.max_avg:
            avg_levels = self.args.avg_levels
            if pos.avg_count < len(avg_levels):
                rise_pct = (h - pos.initial_entry_price) / pos.initial_entry_price * 100
                if rise_pct >= avg_levels[pos.avg_count]:
                    # Моделирование IOC усреднения: может не исполниться
                    if random.random() > self.args.fill_rate:
                        self.avg_failures += 1
                    else:
                        add_vol = self.args.position
                        if self.balance >= add_vol * 0.15:
                            # Проскальзывание при усреднении
                            avg_price = h
                            if self.args.slippage > 0:
                                slip = random.uniform(0, self.args.slippage)
                                avg_price = h * (1 + slip / 100)

                            fee = add_vol * self.args.fee
                            self.total_fees += fee
                            self.balance -= fee
                            self.total_volume += add_vol
                            old_vol = pos.volume_usdt
                            new_avg = (pos.entry_price * old_vol + avg_price * add_vol) / (old_vol + add_vol)
                            pos.entry_price = new_avg
                            pos.volume_usdt += add_vol
                            pos.avg_count += 1

        # TP check — ЛИМИТНЫЙ IOC ордер (как в боте)
        # Лимитка исполняется только если цена дошла до TP уровня
        # Цена закрытия = TP цена (гарантия лимитного ордера, не хуже)
        tp_price = pos.entry_price * (1 - self.args.tp / 100)
        if l <= tp_price:
            # Low свечи ниже/равен TP цене — лимитный ордер исполнится по TP цене
            self.close_position(symbol, tp_price, 'tp', ts)
            return 'tp'

        # SL check (опционально — в боте по умолчанию ВЫКЛ)
        if self.args.sl > 0:
            sl_change = (h - pos.entry_price) / pos.entry_price * 100
            if sl_change >= self.args.sl:
                # SL закрывается маркетом — с проскальзыванием
                sl_price = h  # worst case — по high свечи
                self.close_position(symbol, sl_price, 'sl', ts)
                return 'sl'

        # Timeout (как в боте — закрытие маркетом по текущей цене)
        hold = (ts - pos.opened_at).total_seconds() / 3600
        if hold >= self.args.timeout_hours:
            self.close_position(symbol, c, 'timeout', ts)
            return 'timeout'

        return None


async def run_backtest(args):
    print("=" * 70)
    print("  БЭКТЕСТ: SHORT НА НОВЫХ ЛИСТИНГАХ GATE.IO")
    print("  (реалистичная модель — IOC ордера, проскальзывание)")
    print("=" * 70)
    print()
    print(f"Период:        последние {args.months} мес")
    print(f"TP:            {args.tp}% (лимитный IOC)")
    print(f"SL:            {args.sl}% {'(ВКЛ — маркет)' if args.sl > 0 else '(ВЫКЛ — как в боте)'}")
    print(f"Усреднение:    {args.max_avg}x при {args.avg_levels}")
    print(f"Позиция:       ${args.position}")
    print(f"Макс открытых: {args.max_positions}")
    print(f"Задержка входа:{args.delay} мин")
    print(f"Переоткрытие:  {'да' if args.reopen else 'нет'}")
    print(f"Баланс:        ${args.balance}")
    print(f"Проскальзыв.:  {args.slippage}%")
    print(f"Вероятн. fill: {args.fill_rate*100:.0f}%")
    print(f"Таймаут:       {args.timeout_hours}ч")
    print(f"Seed:          {args.seed}")
    print()

    random.seed(args.seed)

    now = datetime.utcnow()
    start_dt = now - timedelta(days=args.months * 30)
    start_ts = int(start_dt.timestamp())
    end_ts = int(now.timestamp())

    timeout = aiohttp.ClientTimeout(total=600)
    async with aiohttp.ClientSession(timeout=timeout) as session:

        # 1. Fetch contracts
        print("[1/3] Загрузка списка контрактов...")
        contracts = await fetch_contracts(session)
        if not contracts:
            print("ОШИБКА: не удалось загрузить контракты")
            return

        # Filter new crypto listings
        listings = []
        for c in contracts:
            symbol = c.get('name', '')
            create_time = int(c.get('create_time', 0) or 0)
            launch_time = int(c.get('launch_time', 0) or 0)

            if not symbol or create_time < start_ts or create_time > end_ts:
                continue
            if is_filtered(symbol, c):
                continue
            if c.get('status') not in ('trading',) and int(c.get('trade_size', 0) or 0) == 0:
                continue

            listings.append({
                'symbol': symbol,
                'create_time': create_time,
                'launch_time': launch_time if launch_time > 0 else create_time,
                'create_dt': datetime.utcfromtimestamp(create_time),
            })

        listings.sort(key=lambda x: x['create_time'])
        print(f"Найдено {len(listings)} новых крипто-листингов")
        if not listings:
            print("Нет данных для бэктеста")
            return
        print()

        # 2. Fetch candles & simulate
        print(f"[2/3] Загрузка свечей и симуляция...")
        bt = Backtester(args)
        skipped = 0

        for idx, listing in enumerate(listings):
            symbol = listing['symbol']
            candle_start = listing['launch_time']
            candle_end = min(candle_start + args.days_limit * 86400, end_ts)

            if idx % 5 == 0 or idx == len(listings) - 1:
                sys.stdout.write(
                    f"\r  [{idx+1}/{len(listings)}] {symbol:20s} "
                    f"баланс=${bt.balance:.2f} сделок={len(bt.trades)}"
                )
                sys.stdout.flush()

            raw = await fetch_candles(session, symbol, candle_start, candle_end, '5m')
            candles = parse_candles(raw)

            if len(candles) < 5:
                skipped += 1
                continue

            # ATH from available data
            ath = max(c[2] for c in candles)

            # Entry point: first candle after delay
            entry_idx = 0
            if args.delay > 0:
                entry_time = candle_start + args.delay * 60
                for i, (t, *_) in enumerate(candles):
                    if t >= entry_time:
                        entry_idx = i
                        break

            if entry_idx >= len(candles) - 2:
                skipped += 1
                continue

            entry_price = candles[entry_idx][4]  # close price
            entry_dt = datetime.utcfromtimestamp(candles[entry_idx][0])

            if entry_price <= 0:
                skipped += 1
                continue

            # ATH ratio check
            if ath > 0 and entry_price / ath < args.ath_ratio:
                continue

            # Open position (may fail due to fill rate)
            if not bt.open_position(symbol, entry_price, entry_dt):
                continue

            # Process each candle
            for ci in range(entry_idx + 1, len(candles)):
                t, o, h, l, cl, v = candles[ci]
                candle_dt = datetime.utcfromtimestamp(t)
                result = bt.process_candle(symbol, h, l, cl, candle_dt)

                if result is not None and args.reopen and result == 'tp':
                    # Reopen after TP
                    remaining = len(candles) - ci - 1
                    if remaining > 5 and symbol not in bt.positions:
                        bt.reopen_count += 1
                        bt.open_position(symbol, cl, candle_dt)

            # Close if still open at end of data (маркетом, как таймаут)
            if symbol in bt.positions:
                last_t, _, _, _, last_c, _ = candles[-1]
                bt.close_position(symbol, last_c, 'end_of_data',
                                  datetime.utcfromtimestamp(last_t))

        print()
        print()

        # 3. Report
        print("=" * 70)
        print("  РЕЗУЛЬТАТЫ")
        print("=" * 70)
        print()

        trades = bt.trades
        total = len(trades)
        if total == 0:
            print("Сделок не было!")
            # Save empty report
            report = {
                'params': _build_params_dict(args),
                'results': _empty_results(args),
                'trades': [],
                'monthly_pnl': {},
            }
            with open('backtest_report.json', 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            return

        wins = [t for t in trades if t.pnl >= 0]
        losses = [t for t in trades if t.pnl < 0]
        win_rate = len(wins) / total * 100
        total_win = sum(t.pnl for t in wins)
        total_loss = sum(t.pnl for t in losses)
        pf = abs(total_win / total_loss) if total_loss != 0 else float('inf')
        avg_hold = sum(t.hold_hours for t in trades) / total
        tp_count = sum(1 for t in trades if t.reason == 'tp')
        sl_count = sum(1 for t in trades if t.reason == 'sl')
        to_count = sum(1 for t in trades if t.reason == 'timeout')
        eod_count = sum(1 for t in trades if t.reason == 'end_of_data')
        avg_count = sum(1 for t in trades if t.avg_count > 0)
        total_avgs = sum(t.avg_count for t in trades)
        total_pnl = bt.balance - bt.initial_balance

        print(f"Стартовый баланс:  ${bt.initial_balance:.2f}")
        print(f"Конечный баланс:   ${bt.balance:.2f}")
        print(f"Итого PnL:         ${total_pnl:+.2f} ({total_pnl/bt.initial_balance*100:+.1f}%)")
        print(f"Макс просадка:     ${bt.max_drawdown:.2f} ({bt.max_drawdown/bt.initial_balance*100:.1f}%)")
        print(f"Пик баланса:       ${bt.peak_balance:.2f}")
        print(f"Комиссии:          ${bt.total_fees:.2f}")
        print(f"Объём торгов:      ${bt.total_volume:.2f}")
        print()
        print(f"Всего сделок:      {total}")
        print(f"Прибыльных:        {len(wins)} ({win_rate:.1f}%)")
        print(f"Убыточных:         {len(losses)} ({100-win_rate:.1f}%)")
        print(f"Profit Factor:     {pf:.2f}")
        print(f"Сред. удержание:   {avg_hold:.1f}ч")
        print()
        print(f"Закрытий по TP:    {tp_count} (лимитный)")
        print(f"Закрытий по SL:    {sl_count} (маркет)")
        print(f"По таймауту:       {to_count} (маркет)")
        print(f"Конец данных:      {eod_count}")
        print(f"Переоткрытий:      {bt.reopen_count}")
        print()
        print(f"Монет торговано:   {len(bt.symbols_traded)}")
        print(f"Пропущено:         {skipped}")
        print(f"С усреднением:     {avg_count} ({total_avgs} усредн. всего)")
        print()
        print(f"--- Статистика реалистичности ---")
        print(f"Неисполн. входов:  {bt.entry_failures}")
        print(f"Неисполн. усредн.: {bt.avg_failures}")
        print()
        print(f"Лучшая сделка:     ${max(t.pnl for t in trades):+.2f}")
        print(f"Худшая сделка:     ${min(t.pnl for t in trades):+.2f}")
        print()

        # Top 10 worst
        worst = sorted(trades, key=lambda t: t.pnl)[:10]
        print("-" * 70)
        print("  ТОП-10 ХУДШИХ СДЕЛОК")
        print("-" * 70)
        for i, t in enumerate(worst, 1):
            print(f"  {i:2d}. {t.symbol:18s} ${t.pnl:+8.2f} ({t.pnl_pct:+6.1f}%) "
                  f"entry=${t.entry:.4f} exit=${t.exit_price:.4f} "
                  f"avg={t.avg_count} adv=+{t.max_adverse:.0f}% "
                  f"[{t.reason}] {t.hold_hours:.0f}h")
        print()

        # Top 10 best
        best = sorted(trades, key=lambda t: t.pnl, reverse=True)[:10]
        print("-" * 70)
        print("  ТОП-10 ЛУЧШИХ СДЕЛОК")
        print("-" * 70)
        for i, t in enumerate(best, 1):
            print(f"  {i:2d}. {t.symbol:18s} ${t.pnl:+8.2f} ({t.pnl_pct:+6.1f}%) "
                  f"entry=${t.entry:.4f} exit=${t.exit_price:.4f} "
                  f"[{t.reason}] {t.hold_hours:.0f}h")
        print()

        # Monthly PnL
        monthly = {}
        if bt.daily_pnl:
            print("-" * 70)
            print("  ПОМЕСЯЧНЫЙ PnL")
            print("-" * 70)
            for day, pnl in sorted(bt.daily_pnl.items()):
                m = day[:7]
                monthly[m] = monthly.get(m, 0) + pnl
            for m, p in sorted(monthly.items()):
                bar_len = min(int(abs(p)), 50)
                bar = ("+" * bar_len) if p > 0 else ("-" * bar_len)
                print(f"  {m}: ${p:+8.2f}  {bar}")
            print()

        # Save JSON report
        report = {
            'params': _build_params_dict(args),
            'results': {
                'start_balance': bt.initial_balance,
                'end_balance': round(bt.balance, 2),
                'total_pnl': round(total_pnl, 2),
                'max_drawdown': round(bt.max_drawdown, 2),
                'total_trades': total,
                'win_rate': round(win_rate, 1),
                'profit_factor': round(pf, 2),
                'fees': round(bt.total_fees, 2),
                'reopens': bt.reopen_count,
                'entry_failures': bt.entry_failures,
                'avg_failures': bt.avg_failures,
            },
            'trades': [
                {
                    'symbol': t.symbol, 'pnl': round(t.pnl, 4),
                    'pnl_pct': round(t.pnl_pct, 2), 'entry': t.entry,
                    'exit': t.exit_price, 'reason': t.reason,
                    'avg_count': t.avg_count, 'hold_hours': round(t.hold_hours, 1),
                    'max_adverse_pct': round(t.max_adverse, 1), 'volume': t.volume,
                }
                for t in trades
            ],
            'monthly_pnl': {m: round(p, 2) for m, p in sorted(monthly.items())} if monthly else {},
        }
        with open('backtest_report.json', 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"Отчёт сохранён: backtest_report.json")


def _build_params_dict(args) -> dict:
    return {
        'tp': args.tp, 'sl': args.sl, 'position': args.position,
        'balance': args.balance, 'max_positions': args.max_positions,
        'max_avg': args.max_avg, 'avg_levels': args.avg_levels,
        'ath_ratio': args.ath_ratio, 'delay': args.delay,
        'months': args.months, 'days_limit': args.days_limit,
        'timeout_hours': args.timeout_hours, 'fee': args.fee,
        'reopen': args.reopen, 'slippage': args.slippage,
        'fill_rate': args.fill_rate, 'seed': args.seed,
    }


def _empty_results(args) -> dict:
    return {
        'start_balance': args.balance, 'end_balance': args.balance,
        'total_pnl': 0, 'max_drawdown': 0, 'total_trades': 0,
        'win_rate': 0, 'profit_factor': 0, 'fees': 0, 'reopens': 0,
        'entry_failures': 0, 'avg_failures': 0,
    }


def main():
    parser = argparse.ArgumentParser(description='Backtest SHORT strategy on Gate.io new listings')
    parser.add_argument('--tp', type=float, default=2.0, help='Take profit %% (default: 2.0)')
    parser.add_argument('--sl', type=float, default=0.0, help='Stop loss %% (default: 0 = off, как в боте)')
    parser.add_argument('--position', type=float, default=5.0, help='Position size USDT (default: 5)')
    parser.add_argument('--balance', type=float, default=100.0, help='Starting balance (default: 100)')
    parser.add_argument('--max-positions', type=int, default=10, help='Max concurrent (default: 10)')
    parser.add_argument('--max-avg', type=int, default=3, help='Max averagings (default: 3)')
    parser.add_argument('--avg-levels', type=int, nargs='+', default=[300, 700, 1000], help='Avg levels %%')
    parser.add_argument('--ath-ratio', type=float, default=0.3, help='Min ATH ratio (default: 0.3)')
    parser.add_argument('--delay', type=int, default=0, help='Entry delay minutes (default: 0)')
    parser.add_argument('--months', type=int, default=6, help='Backtest period months (default: 6)')
    parser.add_argument('--days-limit', type=int, default=30, help='Max days per listing (default: 30)')
    parser.add_argument('--timeout-hours', type=int, default=720, help='Position timeout hours (default: 720)')
    parser.add_argument('--fee', type=float, default=0.00075, help='Taker fee (default: 0.00075)')
    parser.add_argument('--no-reopen', action='store_true', help='Disable reopen after TP')
    parser.add_argument('--no-avg', action='store_true', help='Disable averaging')
    # Реалистичные параметры (модель IOC ордеров)
    parser.add_argument('--slippage', type=float, default=0.5,
                        help='Max entry slippage %% (default: 0.5, 0=off)')
    parser.add_argument('--fill-rate', type=float, default=0.85,
                        help='IOC fill probability 0-1 (default: 0.85)')
    parser.add_argument('--seed', type=int, default=42,
                        help='Random seed for reproducibility (default: 42)')
    args = parser.parse_args()
    args.reopen = not args.no_reopen
    if args.no_avg:
        args.max_avg = 0

    asyncio.run(run_backtest(args))


if __name__ == '__main__':
    main()
