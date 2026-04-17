"""
Скачать историю funding rates с Binance USDT-M, Bybit linear, OKX SWAP.

Публичные REST endpoints, без API ключа.

API форматы:
    Binance: GET /fapi/v1/fundingRate?symbol=X&startTime=ms&endTime=ms&limit=1000
             returns ASC list of {symbol, fundingTime, fundingRate, markPrice}
    Bybit:   GET /v5/market/funding/history?category=linear&symbol=X&startTime=ms&endTime=ms&limit=200
             returns DESC list of {symbol, fundingRate, fundingRateTimestamp}
    OKX:     GET /api/v5/public/funding-rate-history?instId=X-USDT-SWAP&before=ms&after=ms&limit=100
             returns DESC list of {instId, fundingTime, fundingRate, ...}

Выход:
    data/raw/funding/{exchange}/{base_asset}.parquet
    columns: ts_ms, funding_rate, mark_price (если есть)

Пример:
    python scripts/research/download_funding.py --months 12
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

OUT = Path("data/raw/funding")
USER_AGENT = "Mozilla/5.0 trading-research"

# 20 base assets для ресёрча. Формирование биржевых символов — по правилам в get_symbol()
BASE_ASSETS = [
    "BTC", "ETH", "SOL", "XRP", "DOGE",
    "ADA", "AVAX", "LINK", "DOT", "ARB",
    "OP",  "APT", "SUI", "NEAR", "ATOM",
    "LTC", "BCH", "TRX", "PEPE", "TON",
]


def get_symbol(exchange: str, base: str) -> str:
    """BTC -> BTCUSDT / BTCUSDT / BTC-USDT-SWAP"""
    if exchange == "binance":
        return f"{base}USDT"
    if exchange == "bybit":
        return f"{base}USDT"
    if exchange == "okx":
        return f"{base}-USDT-SWAP"
    raise ValueError(exchange)


def http_get_json(url: str, timeout: float = 20.0) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_binance(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Binance: walk ASC, paginate forward через startTime."""
    url_tmpl = (
        "https://fapi.binance.com/fapi/v1/fundingRate"
        "?symbol={sym}&startTime={st}&endTime={en}&limit=1000"
    )
    out = []
    cur = start_ms
    seen_last = -1
    while cur < end_ms:
        url = url_tmpl.format(sym=symbol, st=cur, en=end_ms)
        try:
            data = http_get_json(url)
        except Exception as e:
            print(f"    binance {symbol}: page error: {e}")
            break
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        last_ft = int(data[-1]["fundingTime"])
        if last_ft <= seen_last:
            break  # no progress, avoid infinite loop
        seen_last = last_ft
        cur = last_ft + 1
        if len(data) < 1000:
            break
        time.sleep(0.1)
    return out


def fetch_bybit(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Bybit: walk DESC, paginate backward через endTime."""
    url_tmpl = (
        "https://api.bybit.com/v5/market/funding/history"
        "?category=linear&symbol={sym}&startTime={st}&endTime={en}&limit=200"
    )
    out = []
    cur_end = end_ms
    seen_first = -1
    while cur_end > start_ms:
        url = url_tmpl.format(sym=symbol, st=start_ms, en=cur_end)
        try:
            resp = http_get_json(url)
        except Exception as e:
            print(f"    bybit {symbol}: page error: {e}")
            break
        if not isinstance(resp, dict) or resp.get("retCode") != 0:
            print(f"    bybit {symbol}: bad response retCode={resp.get('retCode') if isinstance(resp, dict) else '?'}")
            break
        records = resp.get("result", {}).get("list", [])
        if not records:
            break
        out.extend(records)
        oldest = int(records[-1]["fundingRateTimestamp"])
        if oldest >= seen_first and seen_first != -1:
            break
        seen_first = oldest
        cur_end = oldest - 1
        if len(records) < 200:
            break
        time.sleep(0.1)
    return out


def fetch_okx(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """OKX: walk DESC через `after=ms` (записи со временем < after)."""
    url_tmpl = (
        "https://www.okx.com/api/v5/public/funding-rate-history"
        "?instId={sym}&before={bf}&after={af}&limit=100"
    )
    out = []
    cur_after = end_ms
    seen_first = -1
    while cur_after > start_ms:
        url = url_tmpl.format(sym=symbol, bf=start_ms, af=cur_after)
        try:
            resp = http_get_json(url)
        except Exception as e:
            print(f"    okx {symbol}: page error: {e}")
            break
        if not isinstance(resp, dict) or resp.get("code") != "0":
            print(f"    okx {symbol}: bad response code={resp.get('code') if isinstance(resp, dict) else '?'}")
            break
        records = resp.get("data", [])
        if not records:
            break
        out.extend(records)
        oldest = int(records[-1]["fundingTime"])
        if oldest >= seen_first and seen_first != -1:
            break
        seen_first = oldest
        cur_after = oldest - 1
        if len(records) < 100:
            break
        time.sleep(0.2)  # OKX стрже с rate limit
    return out


def to_df(exchange: str, records: list[dict]) -> pd.DataFrame:
    """Привести к унифицированной схеме: ts_ms, funding_rate, mark_price (opt)."""
    if not records:
        return pd.DataFrame(columns=["ts_ms", "funding_rate", "mark_price"])
    if exchange == "binance":
        rows = [
            (int(r["fundingTime"]), float(r["fundingRate"]), float(r.get("markPrice", "nan")))
            for r in records
        ]
    elif exchange == "bybit":
        rows = [
            (int(r["fundingRateTimestamp"]), float(r["fundingRate"]), float("nan"))
            for r in records
        ]
    elif exchange == "okx":
        rows = [
            (int(r["fundingTime"]), float(r["fundingRate"]), float("nan"))
            for r in records
        ]
    else:
        raise ValueError(exchange)
    df = pd.DataFrame(rows, columns=["ts_ms", "funding_rate", "mark_price"])
    df = df.drop_duplicates("ts_ms").sort_values("ts_ms").reset_index(drop=True)
    return df


def download_one(exchange: str, base: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    symbol = get_symbol(exchange, base)
    t0 = time.time()
    if exchange == "binance":
        records = fetch_binance(symbol, start_ms, end_ms)
    elif exchange == "bybit":
        records = fetch_bybit(symbol, start_ms, end_ms)
    elif exchange == "okx":
        records = fetch_okx(symbol, start_ms, end_ms)
    else:
        raise ValueError(exchange)
    df = to_df(exchange, records)
    elapsed = time.time() - t0
    print(f"    {exchange:7s} {base:5s}: {len(df):>5} records, {elapsed:.1f}s")
    return df


def main(months: int) -> int:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - months * 31 * 86_400_000  # ~months месяцев

    print(f"Funding rate download: {months} months window")
    print(
        f"Range: "
        f"{datetime.fromtimestamp(start_ms/1000, tz=timezone.utc)} .. "
        f"{datetime.fromtimestamp(now_ms/1000, tz=timezone.utc)}"
    )
    print(f"Output dir: {OUT.resolve()}")

    for exchange in ["binance", "bybit", "okx"]:
        print(f"\n=== {exchange} ===")
        exch_dir = OUT / exchange
        exch_dir.mkdir(parents=True, exist_ok=True)
        for base in BASE_ASSETS:
            out_path = exch_dir / f"{base}.parquet"
            if out_path.exists():
                print(f"    {exchange:7s} {base:5s}: skip, уже есть")
                continue
            try:
                df = download_one(exchange, base, start_ms, now_ms)
            except Exception as e:
                print(f"    {exchange} {base}: FAIL {type(e).__name__}: {e}")
                continue
            if df.empty:
                print(f"    {exchange} {base}: empty — возможно, пары нет")
                continue
            df.to_parquet(out_path, index=False)

    return 0


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12)
    args = ap.parse_args()
    sys.exit(main(args.months))


if __name__ == "__main__":
    cli()
