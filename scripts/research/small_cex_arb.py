"""
C3: Cross-exchange arb research — мелкие CEX vs Binance.

Гипотеза: MMs не colocated на мелких биржах → арб-окна длиннее.

Скачиваем trades через CCXT (fetchTrades), нормализуем в per-second,
запускаем cross-exchange detector Binance vs small_exchange.

Пример:
    python scripts/research/small_cex_arb.py --date 2026-03-01 --hours 6
"""
from __future__ import annotations

import argparse
import time
import sys
from pathlib import Path
from datetime import datetime, timezone

import ccxt
import pandas as pd

OUT = Path("data/processed/arb_research")
REPORTS = Path("data/reports")

SMALL_EXCHANGES = ["mexc", "gateio", "bitget"]
SYMBOLS = ["ETH/USDT", "BTC/USDT"]
FEE_BPS = 20.0  # conservative: 10bps per side
STALE_SEC = 30


def fetch_trades_ccxt(
    exchange_id: str,
    symbol: str,
    start_ts_ms: int,
    end_ts_ms: int,
    limit_per_call: int = 1000,
) -> pd.DataFrame:
    """Fetch trades via CCXT fetchTrades with pagination.

    Handles exchange-specific quirks (MEXC needs 'until', etc.)
    """
    exchange_class = getattr(ccxt, exchange_id)
    params = {"enableRateLimit": True}
    exchange = exchange_class(params)

    all_trades = []
    since = start_ts_ms
    calls = 0
    max_calls = 500

    while since < end_ts_ms and calls < max_calls:
        try:
            extra_params = {}
            if exchange_id == "mexc":
                # MEXC limits to 1 hour between start and end
                extra_params["until"] = min(since + 3600 * 1000, end_ts_ms)
            trades = exchange.fetch_trades(
                symbol, since=since, limit=limit_per_call, params=extra_params
            )
        except Exception as e:
            # Some exchanges don't support `since` for old dates.
            # Try without `since` to get recent trades.
            if calls == 0:
                try:
                    trades = exchange.fetch_trades(symbol, limit=limit_per_call)
                    if trades and trades[0]["timestamp"] < start_ts_ms:
                        print(f"\n  {exchange_id}: trades too old, no data for {datetime.fromtimestamp(start_ts_ms/1000, tz=timezone.utc).date()}")
                        break
                except Exception:
                    pass
            print(f"\n  Error fetching {exchange_id} {symbol}: {e}")
            break

        if not trades:
            break

        for t in trades:
            if t["timestamp"] >= end_ts_ms:
                since = end_ts_ms  # force stop
                break
            if t["timestamp"] >= start_ts_ms:
                all_trades.append({
                    "ts_ms": t["timestamp"],
                    "price": t["price"],
                    "qty": t["amount"],
                    "side": t["side"],
                })

        last_ts = trades[-1]["timestamp"]
        if last_ts <= since:
            since += 1000
        else:
            since = last_ts + 1

        calls += 1
        sys.stdout.write(
            f"\r  {exchange_id} {symbol}: {len(all_trades):,} trades, "
            f"calls={calls}, up to {datetime.fromtimestamp(min(since, end_ts_ms)/1000, tz=timezone.utc).strftime('%H:%M')}"
        )
        sys.stdout.flush()

    sys.stdout.write("\n")
    if not all_trades:
        return pd.DataFrame()

    return pd.DataFrame(all_trades)


def trades_to_per_second(df: pd.DataFrame) -> pd.DataFrame:
    """Convert raw trades to per-second bid/ask proxy (same format as normalize_trades.py)."""
    if df.empty:
        return pd.DataFrame()

    df["second_ms"] = (df["ts_ms"] // 1000) * 1000

    sells = df[df["side"] == "sell"]
    buys = df[df["side"] == "buy"]

    sell_agg = sells.groupby("second_ms").agg(
        bid_proxy=("price", "max"),
    )
    buy_agg = buys.groupby("second_ms").agg(
        ask_proxy=("price", "min"),
    )

    combined = sell_agg.join(buy_agg, how="outer")
    return combined.reset_index()


def detect_arb_windows(
    ref: pd.DataFrame,
    small: pd.DataFrame,
    ref_name: str,
    small_name: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Detect arb windows between reference (Binance) and small exchange."""
    # Align on second_ms
    ref = ref.set_index("second_ms").sort_index()
    small = small.set_index("second_ms").sort_index()

    # Full range — only overlapping period
    start = max(ref.index.min(), small.index.min())
    end = min(ref.index.max(), small.index.max())
    if pd.isna(start) or pd.isna(end) or start >= end:
        return pd.DataFrame(), pd.DataFrame()
    start = int(start)
    end = int(end)
    full_idx = pd.RangeIndex(start=start, stop=end + 1000, step=1000, name="second_ms")

    ref = ref.reindex(full_idx)
    small = small.reindex(full_idx)

    # Forward fill with staleness cap
    for col in ref.columns:
        ref[col] = ref[col].ffill(limit=STALE_SEC)
    for col in small.columns:
        small[col] = small[col].ffill(limit=STALE_SEC)

    # Arb detection: buy on cheaper, sell on more expensive
    # Direction 1: buy small, sell ref → profit = ref_bid - small_ask
    arb1_bps = (ref["bid_proxy"] - small["ask_proxy"]) / small["ask_proxy"] * 10_000 - FEE_BPS
    # Direction 2: buy ref, sell small → profit = small_bid - ref_ask
    arb2_bps = (small["bid_proxy"] - ref["ask_proxy"]) / ref["ask_proxy"] * 10_000 - FEE_BPS

    best_bps = pd.DataFrame({
        "buy_small_bps": arb1_bps,
        "buy_ref_bps": arb2_bps,
    })
    best_bps["best_bps"] = best_bps.max(axis=1)
    best_bps["direction"] = best_bps.apply(
        lambda r: f"buy_{small_name}" if r["buy_small_bps"] > r["buy_ref_bps"]
        else f"buy_{ref_name}", axis=1
    )

    # Filter profitable seconds
    arb = best_bps[best_bps["best_bps"] > 0].copy()

    # Group into windows
    if arb.empty:
        return arb, pd.DataFrame()

    ts = arb.index.to_series()
    gap = ts.diff().gt(1000)
    window_id = gap.cumsum()

    windows = arb.assign(wid=window_id).groupby("wid").agg(
        start_ms=("best_bps", lambda s: s.index.min()),
        end_ms=("best_bps", lambda s: s.index.max()),
        max_bps=("best_bps", "max"),
        median_bps=("best_bps", "median"),
        direction=("direction", "first"),
    )
    windows["duration_s"] = ((windows["end_ms"] - windows["start_ms"]) // 1000) + 1

    return arb, windows.reset_index(drop=True)


def run(date: str, hours: int = 6) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)

    if date == "recent":
        # Use last N hours from now
        now_ms = int(time.time() * 1000)
        end_ms = now_ms
        start_ms = now_ms - hours * 3600 * 1000
        date_label = datetime.fromtimestamp(start_ms/1000, tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"=== Small CEX arb research — RECENT ({hours}h ending now) ===\n")
    else:
        dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_ms = int(dt.timestamp() * 1000)
        end_ms = start_ms + hours * 3600 * 1000
        date_label = date
        print(f"=== Small CEX arb research — {date} ({hours}h window) ===\n")

    # Load Binance reference (from our existing data if available)
    binance_parquet = OUT / f"binance_ETHUSDT_2026-03.parquet"

    results = []

    for symbol in SYMBOLS:
        bn_symbol = symbol.replace("/", "")
        print(f"\n{'='*60}")
        print(f"  {symbol}")
        print(f"{'='*60}")

        # Get Binance reference
        ref_path = OUT / f"binance_{bn_symbol}_2026-03.parquet"
        if date != "recent" and ref_path.exists():
            print(f"  Loading Binance reference from {ref_path}")
            ref_full = pd.read_parquet(ref_path)
            ref_full = ref_full[
                (ref_full["second_ms"] >= start_ms) &
                (ref_full["second_ms"] < end_ms)
            ]
            ref_sec = ref_full[["second_ms", "bid_proxy", "ask_proxy"]].copy()
            print(f"  Binance: {len(ref_sec):,} seconds")
        else:
            print(f"  Fetching Binance trades via CCXT...")
            raw = fetch_trades_ccxt("binance", symbol, start_ms, end_ms)
            if raw.empty:
                print(f"  No Binance trades, skipping {symbol}")
                continue
            ref_sec = trades_to_per_second(raw)
            print(f"  Binance: {len(ref_sec):,} seconds, {len(raw):,} trades")

        for exch_id in SMALL_EXCHANGES:
            print(f"\n  --- {exch_id} vs binance ({symbol}) ---")

            # Check if we already have data
            cache_path = OUT / f"small_cex_{exch_id}_{bn_symbol}_{date_label}.parquet"
            if cache_path.exists():
                print(f"  Loading cached: {cache_path}")
                small_sec = pd.read_parquet(cache_path)
            else:
                raw = fetch_trades_ccxt(exch_id, symbol, start_ms, end_ms)
                if raw.empty:
                    print(f"  No trades from {exch_id}, skipping")
                    results.append({
                        "exchange": exch_id, "symbol": symbol,
                        "trades": 0, "arb_seconds": 0, "windows": 0,
                        "note": "no trades",
                    })
                    continue
                small_sec = trades_to_per_second(raw)
                # Cache
                small_sec.to_parquet(cache_path, index=False)
                print(f"  {exch_id}: {len(small_sec):,} seconds, {len(raw):,} trades")

            # Detect arb
            arb, windows = detect_arb_windows(ref_sec, small_sec, "binance", exch_id)

            n_arb = len(arb)
            n_win = len(windows)
            hours_actual = hours

            r = {
                "exchange": exch_id,
                "symbol": symbol,
                "trades": len(small_sec),
                "arb_seconds": n_arb,
                "windows": n_win,
                "windows_per_day": n_win / hours_actual * 24 if hours_actual > 0 else 0,
            }

            if not windows.empty:
                r["median_duration_s"] = windows["duration_s"].median()
                r["max_duration_s"] = windows["duration_s"].max()
                r["median_bps"] = windows["median_bps"].median()
                r["p90_bps"] = windows["max_bps"].quantile(0.9)
                r["pct_gte5s"] = (windows["duration_s"] >= 5).mean() * 100
                r["pct_gte30s"] = (windows["duration_s"] >= 30).mean() * 100

                print(f"  Arb seconds: {n_arb:,}, windows: {n_win:,}")
                print(f"  Duration: median={r['median_duration_s']:.0f}s, max={r['max_duration_s']:.0f}s")
                print(f"  Net bps: median={r['median_bps']:.1f}, p90={r['p90_bps']:.1f}")
                print(f"  >= 5s: {r['pct_gte5s']:.1f}%, >= 30s: {r['pct_gte30s']:.1f}%")
                print(f"  Extrapolated: {r['windows_per_day']:.0f} windows/day")
            else:
                r["median_duration_s"] = 0
                r["max_duration_s"] = 0
                r["median_bps"] = 0
                r["p90_bps"] = 0
                r["pct_gte5s"] = 0
                r["pct_gte30s"] = 0
                print(f"  No arb windows found")

            results.append(r)

    # Report
    df_results = pd.DataFrame(results)
    _write_report(date_label, hours, df_results)


def _write_report(date: str, hours: int, df: pd.DataFrame) -> None:
    lines = [
        f"# Small CEX arb research — {date}",
        "",
        f"**Period:** {hours} hours starting 00:00 UTC",
        f"**Reference:** Binance (most liquid)",
        f"**Small exchanges:** {', '.join(SMALL_EXCHANGES)}",
        f"**Fee model:** {FEE_BPS:.0f} bps round-trip",
        f"**Staleness cap:** {STALE_SEC}s forward-fill",
        "",
        "## Summary",
        "",
        "| Exchange | Symbol | Trades | Arb sec | Windows | Win/day | Med dur | Max dur | Med bps | >=5s | >=30s |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, r in df.iterrows():
        lines.append(
            f"| {r['exchange']} | {r['symbol']} | {int(r.get('trades',0)):,} | "
            f"{int(r.get('arb_seconds',0)):,} | {int(r.get('windows',0)):,} | "
            f"{r.get('windows_per_day',0):.0f} | "
            f"{r.get('median_duration_s',0):.0f}s | {r.get('max_duration_s',0):.0f}s | "
            f"{r.get('median_bps',0):.1f} | {r.get('pct_gte5s',0):.1f}% | "
            f"{r.get('pct_gte30s',0):.1f}% |"
        )

    lines += [
        "",
        "## Comparison with Binance-Bybit-OKX (from previous research)",
        "",
        "Previous results (March 2026, full month):",
        "- BTC: 138 windows, 4.4/day, median duration 1s, 95.7% single-second",
        "- ETH: 563 windows, 18/day, median duration 1s, 92.4% single-second",
        "- All windows <=1 second at L1 resolution",
        "",
        "**Hypothesis: small exchanges should show LONGER windows (>=5s, >=30s)**",
        "**because MMs are not colocated and spreads close slower.**",
        "",
    ]

    out = REPORTS / f"small_cex_arb_{date}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {out}")


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--hours", type=int, default=6, help="Hours to analyze (default 6)")
    args = ap.parse_args()
    run(args.date, args.hours)


if __name__ == "__main__":
    cli()
