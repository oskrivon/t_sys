"""
Скачать free L1 book_ticker за 1-й день месяца с Tardis.dev.

Tardis бесплатно отдаёт 1-й день каждого месяца без API ключа. URL:
    https://datasets.tardis.dev/v1/{exchange}/{data_type}/{YYYY}/{MM}/{DD}/{SYMBOL}.csv.gz

Схема (унифицированная для всех бирж):
    exchange, symbol, timestamp(μs), local_timestamp(μs),
    ask_amount, ask_price, bid_price, bid_amount

Для наших бирж slug такие:
    Binance spot = 'binance'
    Bybit spot   = 'bybit-spot'
    OKX spot     = 'okex'

Пример:
    python scripts/research/download_tardis.py --month 2026-03
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

URL_TMPL = (
    "https://datasets.tardis.dev/v1/{exch}/book_ticker/"
    "{yyyy}/{mm}/{dd}/{sym}.csv.gz"
)
OUT_DIR = Path("data/raw/tardis")

# (tardis_slug, tardis_symbol, display_exchange, display_symbol)
SOURCES = [
    ("binance",    "BTCUSDT",  "binance", "BTCUSDT"),
    ("binance",    "ETHUSDT",  "binance", "ETHUSDT"),
    ("bybit-spot", "BTCUSDT",  "bybit",   "BTCUSDT"),
    ("bybit-spot", "ETHUSDT",  "bybit",   "ETHUSDT"),
    ("okex",       "BTC-USDT", "okx",     "BTC-USDT"),
    ("okex",       "ETH-USDT", "okx",     "ETH-USDT"),
]


def human_mb(n_bytes: int) -> str:
    return f"{n_bytes / 1e6:.1f} MB"


def download_stream(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    with urllib.request.urlopen(url, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", "0"))
        done = 0
        chunk = 1 << 20

        with tmp.open("wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if total:
                    pct = done / total * 100
                    sys.stdout.write(
                        f"\r    {human_mb(done)} / {human_mb(total)} ({pct:5.1f}%)"
                    )
                else:
                    sys.stdout.write(f"\r    {human_mb(done)}")
                sys.stdout.flush()
    sys.stdout.write("\n")
    tmp.rename(dest)


def already_downloaded(dest: Path) -> bool:
    return dest.exists() and dest.stat().st_size > 0


def download_one(slug: str, sym: str, display_exch: str, yyyy: str, mm: str, dd: str) -> bool:
    url = URL_TMPL.format(exch=slug, sym=sym, yyyy=yyyy, mm=mm, dd=dd)
    # на диске используем display_exchange для единообразия с proxy
    dest = OUT_DIR / display_exch / sym / f"{sym}-book_ticker-{yyyy}-{mm}-{dd}.csv.gz"
    print(f"[{display_exch} {sym} {yyyy}-{mm}-{dd}]")
    if already_downloaded(dest):
        print(f"    skip, уже есть: {dest} ({human_mb(dest.stat().st_size)})")
        return True
    try:
        download_stream(url, dest)
    except Exception as e:
        print(f"    FAIL: {e}")
        return False
    print(f"    сохранил: {dest}")
    return True


def main(month: str, day: int) -> int:
    yyyy, mm = month.split("-")
    dd = f"{day:02d}"
    print(f"Downloading Tardis free book_ticker for {yyyy}-{mm}-{dd}")
    print(f"Target dir: {OUT_DIR.resolve()}")
    ok = 0
    for slug, sym, display_exch, _ in SOURCES:
        if download_one(slug, sym, display_exch, yyyy, mm, dd):
            ok += 1
    print(f"\n=== Done: {ok}/{len(SOURCES)} успешно ===")
    return 0 if ok == len(SOURCES) else 1


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM (напр. 2026-03)")
    ap.add_argument("--day", type=int, default=1, help="день месяца (по умолчанию 1 — free)")
    args = ap.parse_args()
    sys.exit(main(args.month, args.day))


if __name__ == "__main__":
    cli()
