"""
Скачать monthly spot trades с public.bybit.com (Bybit).

Схема CSV (с заголовком):
    id, timestamp(ms), price, volume, side

side: buy/sell — taker side. Тот же смысл что Binance is_buyer_maker:
    side=sell  → taker продавал (удар по bid) → price ≤ real_best_bid
    side=buy   → taker покупал (удар по ask) → price ≥ real_best_ask

Пример:
    python scripts/research/download_bybit_trades.py --symbols BTCUSDT ETHUSDT --month 2026-03
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

URL_TMPL = "https://public.bybit.com/spot/{sym}/{sym}-{month}.csv.gz"
OUT_DIR = Path("data/raw/bybit")


def human_mb(n_bytes: int) -> str:
    return f"{n_bytes / 1e6:.1f} MB"


def download_stream(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    with urllib.request.urlopen(url, timeout=30) as resp:
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


def already_downloaded(dest: Path, url: str) -> bool:
    if not dest.exists():
        return False
    local_size = dest.stat().st_size
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as r:
            remote_size = int(r.headers.get("Content-Length", "0"))
        if remote_size and local_size == remote_size:
            return True
        print(f"    size mismatch (local={local_size}, remote={remote_size}) — перекачиваю")
        return False
    except Exception as e:
        print(f"    HEAD failed ({e}), но файл есть — пропускаю")
        return True


def download_one(symbol: str, month: str) -> bool:
    url = URL_TMPL.format(sym=symbol, month=month)
    dest = OUT_DIR / symbol / f"{symbol}-{month}.csv.gz"
    print(f"[{symbol} {month}]")
    if already_downloaded(dest, url):
        print(f"    skip, уже есть: {dest} ({human_mb(dest.stat().st_size)})")
        return True
    try:
        download_stream(url, dest)
    except Exception as e:
        print(f"    FAIL: {e}")
        return False
    print(f"    сохранил: {dest}")
    return True


def main(symbols: list[str], month: str) -> int:
    print(f"Downloading Bybit spot trades: symbols={symbols} month={month}")
    print(f"Target dir: {OUT_DIR.resolve()}")
    ok = 0
    for sym in symbols:
        if download_one(sym, month):
            ok += 1
    print(f"\n=== Done: {ok}/{len(symbols)} успешно ===")
    return 0 if ok == len(symbols) else 1


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    ap.add_argument("--month", required=True, help="YYYY-MM (напр. 2026-03)")
    args = ap.parse_args()
    sys.exit(main(args.symbols, args.month))


if __name__ == "__main__":
    cli()
