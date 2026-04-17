"""
Скачать daily spot trades с static.okx.com.

URL: https://static.okx.com/cdn/okex/traderecords/trades/daily/{YYYYMMDD}/{SYM}-trades-{YYYY-MM-DD}.zip

Символ OKX использует дефис: BTC-USDT, ETH-USDT (в отличие от Binance/Bybit без дефиса).

Схема CSV (с заголовком):
    instrument_name, trade_id, side, price, size, created_time(ms)

side: buy/sell (taker side).
    side=sell → taker продавал (удар по bid) → price ≤ real_best_bid
    side=buy  → taker покупал (удар по ask) → price ≥ real_best_ask

Пример:
    python scripts/research/download_okx_trades.py --symbols BTC-USDT ETH-USDT --month 2026-03
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import aiohttp

URL_TMPL = (
    "https://static.okx.com/cdn/okex/traderecords/trades/daily/"
    "{yyyymmdd}/{sym}-trades-{day}.zip"
)
OUT_DIR = Path("data/raw/okx")
CONCURRENCY = 4


def human_mb(n_bytes: int) -> str:
    return f"{n_bytes / 1e6:.1f} MB"


def month_days(month: str) -> list[date]:
    """YYYY-MM → список всех дней месяца."""
    y, m = map(int, month.split("-"))
    d0 = date(y, m, 1)
    d_end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    out = []
    d = d0
    while d < d_end:
        out.append(d)
        d += timedelta(days=1)
    return out


async def download_file(
    session: aiohttp.ClientSession, url: str, dest: Path
) -> tuple[bool, str]:
    if dest.exists() and dest.stat().st_size > 0:
        return True, f"skip ({human_mb(dest.stat().st_size)})"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            data = await resp.read()
            tmp.write_bytes(data)
            tmp.rename(dest)
            return True, f"ok ({human_mb(len(data))})"
    except Exception as e:
        return False, f"error: {e}"


async def download_symbol_month(
    session: aiohttp.ClientSession, symbol: str, month: str
) -> tuple[int, int]:
    """Возвращает (ok_count, total)."""
    days = month_days(month)
    sem = asyncio.Semaphore(CONCURRENCY)
    results: list[tuple[date, bool, str]] = []

    async def one(d: date) -> None:
        day_iso = d.isoformat()
        yyyymmdd = day_iso.replace("-", "")
        url = URL_TMPL.format(yyyymmdd=yyyymmdd, sym=symbol, day=day_iso)
        dest = OUT_DIR / symbol / f"{symbol}-trades-{day_iso}.zip"
        async with sem:
            ok, msg = await download_file(session, url, dest)
        results.append((d, ok, msg))
        sys.stdout.write(
            f"\r    {symbol}: {len(results)}/{len(days)}  last: {day_iso} -> {msg}     "
        )
        sys.stdout.flush()

    await asyncio.gather(*[one(d) for d in days])
    sys.stdout.write("\n")

    ok_count = sum(1 for _, ok, _ in results if ok)
    for d, ok, msg in results:
        if not ok:
            print(f"    FAIL {symbol} {d}: {msg}")
    return ok_count, len(days)


async def main(symbols: list[str], month: str) -> int:
    print(f"Downloading OKX spot trades: symbols={symbols} month={month}")
    print(f"Target dir: {OUT_DIR.resolve()}")
    all_ok = True
    async with aiohttp.ClientSession() as session:
        for sym in symbols:
            print(f"[{sym} {month}]")
            ok, total = await download_symbol_month(session, sym, month)
            print(f"    => {ok}/{total} дней")
            if ok < total:
                all_ok = False
    return 0 if all_ok else 1


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--symbols",
        nargs="+",
        default=["BTC-USDT", "ETH-USDT"],
        help="OKX instrument_id с дефисом, напр. BTC-USDT",
    )
    ap.add_argument("--month", required=True, help="YYYY-MM (напр. 2026-03)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.symbols, args.month)))


if __name__ == "__main__":
    cli()
