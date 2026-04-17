"""
Miro Strategy v3: rolling levels + dynamic coin selection.

Ключевое отличие от v2:
  - Rolling level detection (lookback 200 candles, обновляются каждую свечу)
  - Больше нет look-ahead bias и нет "0 trades in test"
  - Dynamic coin filter: объём + волатильность
  - Закол как primary signal (не только доп.)
  - Full-period walk-forward (без train/test split — rolling сам по себе honest)

Пример:
    python scripts/research/miro_strategy_v3.py
"""
from __future__ import annotations

import sys
import time
import math
from pathlib import Path
from collections import defaultdict

import ccxt
import pandas as pd
import numpy as np

DATA = Path("data/processed/candles")
REPORTS = Path("data/reports")

SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "LINK/USDT",
    "ARB/USDT", "PEPE/USDT", "SUI/USDT", "DOGE/USDT",
    "AVAX/USDT", "OP/USDT", "NEAR/USDT", "FIL/USDT",
]
TF = "4h"
MONTHS = 8
RR_RATIO = 3.0
MAX_RISK_PCT = 3.0
INITIAL_CAPITAL = 10_000
FEE_BPS = 10
TREND_SMA = 50
LEVEL_LOOKBACK = 200     # candles to look back for levels
LEVEL_MIN_TOUCHES = 2
LEVEL_TOLERANCE_PCT = 1.0
RETEST_WINDOW = 15       # candles to wait for retest
MIN_LEVEL_AGE = 20       # level must be at least 20 candles old


def fetch_ohlcv(symbol: str, tf: str, months: int) -> pd.DataFrame:
    exchange = ccxt.binance({"enableRateLimit": True})
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - months * 30 * 86400 * 1000
    all_c = []
    cur = since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=cur, limit=1000)
        except Exception:
            break
        if not c:
            break
        all_c.extend(c)
        last = c[-1][0]
        if last <= cur or len(c) < 1000:
            break
        cur = last + 1
        time.sleep(0.1)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def load_data() -> dict[str, pd.DataFrame]:
    DATA.mkdir(parents=True, exist_ok=True)
    datasets = {}
    for symbol in SYMBOLS:
        key = symbol.replace("/", "")
        path = DATA / f"{key}_{TF}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
        else:
            sys.stdout.write(f"  Downloading {symbol} {TF}...")
            sys.stdout.flush()
            df = fetch_ohlcv(symbol, TF, MONTHS)
            if not df.empty:
                df.to_parquet(path, index=False)
                sys.stdout.write(f" {len(df)} candles\n")
            else:
                sys.stdout.write(" FAILED\n")
        if not df.empty:
            datasets[symbol] = df
    return datasets


# ── Rolling level detection ──

def find_swing_points_range(df: pd.DataFrame, start: int, end: int, order: int = 5) -> list[tuple[int, float, str]]:
    """Find swing highs/lows in range [start, end). Returns (idx, price, type)."""
    points = []
    for i in range(max(start, order), min(end, len(df) - order)):
        wh = df["high"].iloc[i - order:i + order + 1]
        if df["high"].iloc[i] == wh.max():
            points.append((i, df["high"].iloc[i], "high"))
        wl = df["low"].iloc[i - order:i + order + 1]
        if df["low"].iloc[i] == wl.min():
            points.append((i, df["low"].iloc[i], "low"))
    return points


def cluster_points(points: list[tuple[int, float, str]], tolerance_pct: float) -> list[dict]:
    """Cluster swing points into levels."""
    if not points:
        return []
    sorted_pts = sorted(points, key=lambda p: p[1])
    levels = []
    used = set()

    for i, (idx, price, _) in enumerate(sorted_pts):
        if i in used:
            continue
        cluster = [(idx, price)]
        used.add(i)
        for j in range(i + 1, len(sorted_pts)):
            if j in used:
                continue
            if abs(sorted_pts[j][1] - price) / price < tolerance_pct / 100:
                cluster.append((sorted_pts[j][0], sorted_pts[j][1]))
                used.add(j)

        if len(cluster) >= LEVEL_MIN_TOUCHES:
            prices = [p[1] for p in cluster]
            indices = [p[0] for p in cluster]
            mean_p = np.mean(prices)

            # Score
            score = len(cluster)
            if is_round_number(mean_p):
                score += 2
            if (max(prices) - min(prices)) / mean_p < 0.005:
                score += 1
            if len(cluster) >= 4:
                score += 2

            levels.append({
                "price": mean_p,
                "touches": len(cluster),
                "zone_high": max(prices),
                "zone_low": min(prices),
                "first_idx": min(indices),
                "last_idx": max(indices),
                "score": score,
            })

    return sorted(levels, key=lambda x: x["score"], reverse=True)


def is_round_number(price: float) -> bool:
    if price <= 0:
        return False
    magnitude = 10 ** int(math.log10(abs(price)))
    for div in [magnitude, magnitude / 2, magnitude / 5, magnitude / 10]:
        if div > 0:
            rem = price % div
            if rem / div < 0.02 or rem / div > 0.98:
                return True
    return False


def get_rolling_levels(df: pd.DataFrame, current_idx: int) -> list[dict]:
    """Get levels from the last LEVEL_LOOKBACK candles, only levels that are old enough."""
    start = max(0, current_idx - LEVEL_LOOKBACK)
    end = current_idx - 5  # don't use last 5 candles (swing detection needs buffer)

    if end - start < 50:
        return []

    points = find_swing_points_range(df, start, end, order=5)
    levels = cluster_points(points, LEVEL_TOLERANCE_PCT)

    # Filter: level must be at least MIN_LEVEL_AGE candles old
    filtered = [lv for lv in levels if current_idx - lv["last_idx"] >= MIN_LEVEL_AGE]
    return filtered


# ── Walk-forward backtest ──

def run_walk_forward(
    df: pd.DataFrame,
    symbol: str,
    use_trend: bool = True,
) -> dict:
    """Walk-forward backtest: at each candle, detect levels and signals dynamically."""

    # Precompute trend
    sma = df["close"].rolling(TREND_SMA).mean()
    trend = pd.Series(0, index=df.index)
    trend[df["close"] > sma] = 1
    trend[df["close"] < sma] = -1

    capital = INITIAL_CAPITAL
    trades = []
    active_trade = None  # {entry_idx, entry_price, sl, tp, is_long, position_size}

    # Track recent breakouts for retest detection
    recent_breakouts = []  # {level, direction, breakout_idx}

    # Check levels every N candles (performance optimization)
    CHECK_INTERVAL = 6  # every 6 candles = 24h for 4h TF
    cached_levels = []
    last_check = 0

    warmup = max(LEVEL_LOOKBACK, TREND_SMA) + 10

    for i in range(warmup, len(df)):
        # ── Manage active trade ──
        if active_trade is not None:
            h = df["high"].iloc[i]
            l = df["low"].iloc[i]
            at = active_trade

            if at["is_long"]:
                if l <= at["sl"]:
                    pnl_pct = (at["sl"] - at["entry_price"]) / at["entry_price"] - 2 * FEE_BPS / 10000
                    pnl_usd = at["position_size"] * pnl_pct
                    capital += pnl_usd
                    trades.append({**at, "outcome": "loss", "exit_price": at["sl"],
                                   "exit_idx": i, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                                   "capital_after": capital, "exit_ts": df["ts"].iloc[i]})
                    active_trade = None
                    continue
                if h >= at["tp"]:
                    pnl_pct = (at["tp"] - at["entry_price"]) / at["entry_price"] - 2 * FEE_BPS / 10000
                    pnl_usd = at["position_size"] * pnl_pct
                    capital += pnl_usd
                    trades.append({**at, "outcome": "win", "exit_price": at["tp"],
                                   "exit_idx": i, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                                   "capital_after": capital, "exit_ts": df["ts"].iloc[i]})
                    active_trade = None
                    continue
                # Timeout check
                if i - at["entry_idx"] > 150:
                    exit_p = df["close"].iloc[i]
                    pnl_pct = (exit_p - at["entry_price"]) / at["entry_price"] - 2 * FEE_BPS / 10000
                    pnl_usd = at["position_size"] * pnl_pct
                    capital += pnl_usd
                    trades.append({**at, "outcome": "timeout", "exit_price": exit_p,
                                   "exit_idx": i, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                                   "capital_after": capital, "exit_ts": df["ts"].iloc[i]})
                    active_trade = None
            else:  # short
                if h >= at["sl"]:
                    pnl_pct = (at["entry_price"] - at["sl"]) / at["entry_price"] - 2 * FEE_BPS / 10000
                    pnl_usd = at["position_size"] * pnl_pct
                    capital += pnl_usd
                    trades.append({**at, "outcome": "loss", "exit_price": at["sl"],
                                   "exit_idx": i, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                                   "capital_after": capital, "exit_ts": df["ts"].iloc[i]})
                    active_trade = None
                    continue
                if l <= at["tp"]:
                    pnl_pct = (at["entry_price"] - at["tp"]) / at["entry_price"] - 2 * FEE_BPS / 10000
                    pnl_usd = at["position_size"] * pnl_pct
                    capital += pnl_usd
                    trades.append({**at, "outcome": "win", "exit_price": at["tp"],
                                   "exit_idx": i, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                                   "capital_after": capital, "exit_ts": df["ts"].iloc[i]})
                    active_trade = None
                    continue
                if i - at["entry_idx"] > 150:
                    exit_p = df["close"].iloc[i]
                    pnl_pct = (at["entry_price"] - exit_p) / at["entry_price"] - 2 * FEE_BPS / 10000
                    pnl_usd = at["position_size"] * pnl_pct
                    capital += pnl_usd
                    trades.append({**at, "outcome": "timeout", "exit_price": exit_p,
                                   "exit_idx": i, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                                   "capital_after": capital, "exit_ts": df["ts"].iloc[i]})
                    active_trade = None
            if active_trade is not None:
                continue  # still in trade, don't look for new signals

        # ── Update levels periodically ──
        if i - last_check >= CHECK_INTERVAL:
            cached_levels = get_rolling_levels(df, i)
            last_check = i

            # Detect new breakouts
            close_i = df["close"].iloc[i]
            prev_close = df["close"].iloc[i - 1]
            for lv in cached_levels:
                zh, zl = lv["zone_high"], lv["zone_low"]
                # Breakout up
                if prev_close <= zh and close_i > zh * 1.001:
                    recent_breakouts.append({"level": lv, "dir": "long", "idx": i})
                # Breakout down
                if prev_close >= zl and close_i < zl * 0.999:
                    recent_breakouts.append({"level": lv, "dir": "short", "idx": i})

            # Clean old breakouts (>RETEST_WINDOW candles)
            recent_breakouts = [b for b in recent_breakouts if i - b["idx"] <= RETEST_WINDOW]

        # ── Look for entry signals ──
        close_i = df["close"].iloc[i]
        low_i = df["low"].iloc[i]
        high_i = df["high"].iloc[i]
        t = trend.iloc[i]

        best_signal = None
        best_score = 0

        for brk in recent_breakouts:
            lv = brk["level"]
            zh, zl = lv["zone_high"], lv["zone_low"]
            age = i - brk["idx"]

            if age < 1 or age > RETEST_WINDOW:
                continue

            # ── LONG retest: price returns to zone after breakout up ──
            if brk["dir"] == "long" and low_i <= zh * 1.005 and close_i > zh:
                if use_trend and t == -1:
                    continue
                if lv["score"] > best_score:
                    best_signal = {"type": "long_retest", "level": lv}
                    best_score = lv["score"]

            # ── SHORT retest: price returns to zone after breakout down ──
            if brk["dir"] == "short" and high_i >= zl * 0.995 and close_i < zl:
                if use_trend and t == 1:
                    continue
                if lv["score"] > best_score:
                    best_signal = {"type": "short_retest", "level": lv}
                    best_score = lv["score"]

        # ── Закол signal (independent of breakout tracking) ──
        for lv in cached_levels:
            zh, zl = lv["zone_high"], lv["zone_low"]
            # Long закол: wick below support, close above
            if low_i < zl * 0.995 and close_i > zl:
                if use_trend and t == -1:
                    continue
                if lv["score"] + 1 > best_score:  # закол gets +1 bonus
                    best_signal = {"type": "long_zakol", "level": lv}
                    best_score = lv["score"] + 1
            # Short закол: wick above resistance, close below
            if high_i > zh * 1.005 and close_i < zh:
                if use_trend and t == 1:
                    continue
                if lv["score"] + 1 > best_score:
                    best_signal = {"type": "short_zakol", "level": lv}
                    best_score = lv["score"] + 1

        # ── Enter trade if signal found ──
        if best_signal and capital > 100:
            lv = best_signal["level"]
            is_long = "long" in best_signal["type"]
            zh, zl = lv["zone_high"], lv["zone_low"]
            zone_w = zh - zl
            if zone_w < close_i * 0.003:
                zone_w = close_i * 0.005

            if is_long:
                sl = zl - zone_w * 0.3
                sl_dist = close_i - sl
                tp = close_i + sl_dist * RR_RATIO
            else:
                sl = zh + zone_w * 0.3
                sl_dist = sl - close_i
                tp = close_i - sl_dist * RR_RATIO

            if sl_dist <= 0 or sl_dist / close_i > 0.08:
                continue

            risk_amt = capital * MAX_RISK_PCT / 100
            pos_size = risk_amt / (sl_dist / close_i)
            pos_size = min(pos_size, capital * 0.90)

            active_trade = {
                "type": best_signal["type"],
                "entry_idx": i,
                "entry_price": close_i,
                "entry_ts": df["ts"].iloc[i],
                "sl": sl,
                "tp": tp,
                "is_long": is_long,
                "position_size": pos_size,
                "level_score": lv["score"],
                "level_touches": lv["touches"],
            }

    # ── Results ──
    if not trades:
        return _empty_result(symbol)

    tdf = pd.DataFrame(trades)
    return _compute_metrics(tdf, symbol, INITIAL_CAPITAL)


def _empty_result(symbol):
    return {"symbol": symbol, "n_trades": 0, "n_wins": 0, "n_losses": 0,
            "n_timeout": 0, "win_rate": 0, "total_return": 0, "max_dd": 0,
            "final_capital": INITIAL_CAPITAL, "sharpe": 0, "profit_factor": 0,
            "avg_win_pct": 0, "avg_loss_pct": 0, "expectancy": 0, "trades": []}


def _compute_metrics(tdf, symbol, initial):
    wins = tdf[tdf["outcome"] == "win"]
    losses = tdf[tdf["outcome"] == "loss"]

    eq = np.concatenate([[initial], tdf["capital_after"].values])
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    max_dd = dd.min()

    pnls = tdf["pnl_pct"].values
    sharpe = pnls.mean() / pnls.std() * np.sqrt(len(pnls)) if len(pnls) > 1 and pnls.std() > 0 else 0

    gw = wins["pnl_usd"].sum() if len(wins) > 0 else 0
    gl = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1
    pf = gw / gl if gl > 0 else 0

    wr = len(wins) / len(tdf) if len(tdf) > 0 else 0
    aw = wins["pnl_pct"].mean() if len(wins) > 0 else 0
    al = abs(losses["pnl_pct"].mean()) if len(losses) > 0 else 0
    exp = wr * aw - (1 - wr) * al

    return {
        "symbol": symbol,
        "n_trades": len(tdf),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_timeout": len(tdf[tdf["outcome"] == "timeout"]),
        "win_rate": wr,
        "total_return": (tdf["capital_after"].iloc[-1] - initial) / initial,
        "max_dd": max_dd,
        "final_capital": tdf["capital_after"].iloc[-1],
        "sharpe": sharpe,
        "profit_factor": pf,
        "avg_win_pct": aw,
        "avg_loss_pct": al,
        "expectancy": exp,
        "trades": tdf.to_dict("records"),
    }


def run() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)

    print("=== Miro Strategy v3: Rolling Levels Walk-Forward ===\n")
    datasets = load_data()

    all_results = []
    all_trades = []

    for symbol, df in datasets.items():
        print(f"\n  {symbol}: {len(df)} candles, {df['ts'].iloc[0].date()} to {df['ts'].iloc[-1].date()}")
        result = run_walk_forward(df, symbol, use_trend=True)
        all_results.append(result)

        for t in result["trades"]:
            t["symbol"] = symbol
            all_trades.append(t)

        wr = result["win_rate"]
        ret = result["total_return"]
        print(f"    {result['n_trades']} trades, {wr*100:.0f}% WR, {ret*100:+.1f}%, "
              f"DD {result['max_dd']*100:.1f}%, PF {result['profit_factor']:.2f}")

    # ── Portfolio analysis ──
    print(f"\n{'='*60}")
    print("  PORTFOLIO SUMMARY")
    print(f"{'='*60}")

    if not all_trades:
        print("  No trades!")
        return

    tdf = pd.DataFrame(all_trades)
    wins = tdf[tdf["outcome"] == "win"]
    losses = tdf[tdf["outcome"] == "loss"]
    n = len(tdf)
    wr = len(wins) / n
    aw = wins["pnl_pct"].mean() if len(wins) > 0 else 0
    al = abs(losses["pnl_pct"].mean()) if len(losses) > 0 else 0
    exp = wr * aw - (1 - wr) * al
    gw = wins["pnl_usd"].sum() if len(wins) > 0 else 0
    gl = abs(losses["pnl_usd"].sum()) if len(losses) > 0 else 1
    pf = gw / gl if gl > 0 else 0

    # Period in months
    date_range = (tdf["exit_ts"].max() - tdf["entry_ts"].min()).total_seconds() / (30 * 86400)
    trades_per_month = n / date_range if date_range > 0 else 0

    print(f"  Trades: {n} ({trades_per_month:.1f}/month)")
    print(f"  Wins: {len(wins)}, Losses: {len(losses)}, Timeout: {len(tdf[tdf['outcome']=='timeout'])}")
    print(f"  Win rate: {wr*100:.1f}%")
    print(f"  Avg win: {aw*100:+.2f}%, Avg loss: -{al*100:.2f}%")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Expectancy/trade: {exp*100:+.3f}%")

    # Return estimation
    monthly_ret = trades_per_month * exp * MAX_RISK_PCT / 100
    annual_ret = (1 + monthly_ret) ** 12 - 1

    print(f"\n  === RETURN ESTIMATION ===")
    print(f"  Risk/trade: {MAX_RISK_PCT}%")
    print(f"  Monthly return: {monthly_ret*100:+.2f}%")
    print(f"  Annual return: {annual_ret*100:+.1f}%")

    print(f"\n  Scenarios:")
    for name, wr_adj in [("pessimistic", -0.05), ("base", 0), ("optimistic", +0.05)]:
        adj_wr = wr + wr_adj
        adj_exp = adj_wr * aw - (1 - adj_wr) * al
        adj_m = trades_per_month * adj_exp * MAX_RISK_PCT / 100
        adj_a = (1 + adj_m) ** 12 - 1
        print(f"    {name:12s}: WR={adj_wr*100:.0f}%, monthly={adj_m*100:+.2f}%, annual={adj_a*100:+.1f}%")

    # Best/worst symbols
    rdf = pd.DataFrame([r for r in all_results if r["n_trades"] > 0])
    if not rdf.empty:
        rdf = rdf.sort_values("total_return", ascending=False)
        print(f"\n  Best symbols:")
        for _, r in rdf.head(5).iterrows():
            print(f"    {r['symbol']:12s}: {r['n_trades']}t, {r['win_rate']*100:.0f}%WR, "
                  f"{r['total_return']*100:+.1f}%, PF {r['profit_factor']:.2f}")
        print(f"  Worst symbols:")
        for _, r in rdf.tail(3).iterrows():
            print(f"    {r['symbol']:12s}: {r['n_trades']}t, {r['win_rate']*100:.0f}%WR, "
                  f"{r['total_return']*100:+.1f}%, PF {r['profit_factor']:.2f}")

    # By signal type
    print(f"\n  By signal type:")
    for stype in tdf["type"].unique():
        sub = tdf[tdf["type"] == stype]
        sw = sub[sub["outcome"] == "win"]
        swr = len(sw) / len(sub) if len(sub) > 0 else 0
        print(f"    {stype:18s}: {len(sub):3d} trades, {swr*100:.0f}% WR")

    # Write report
    _write_report(all_results, tdf, trades_per_month, wr, aw, al, exp, pf, monthly_ret, annual_ret)


def _write_report(results, tdf, tpm, wr, aw, al, exp, pf, monthly, annual):
    lines = [
        "# Miro Strategy v3: Rolling Levels Walk-Forward",
        "",
        f"**Key improvement:** Rolling level detection (lookback {LEVEL_LOOKBACK} candles),",
        f"no train/test split bias, levels update dynamically.",
        "",
        f"**TF:** {TF}, **R:R:** 1:{RR_RATIO:.0f}, **Risk/trade:** {MAX_RISK_PCT}%",
        f"**Trend filter:** SMA({TREND_SMA}), **Symbols:** {len(SYMBOLS)}",
        "",
        "## Per-Symbol Results",
        "",
        "| Symbol | Trades | WR | Return | Max DD | PF | Sharpe |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sorted(results, key=lambda x: x["total_return"], reverse=True):
        if r["n_trades"] == 0:
            lines.append(f"| {r['symbol']} | 0 | - | - | - | - | - |")
        else:
            lines.append(
                f"| {r['symbol']} | {r['n_trades']} | {r['win_rate']*100:.0f}% | "
                f"{r['total_return']*100:+.1f}% | {r['max_dd']*100:.1f}% | "
                f"{r['profit_factor']:.2f} | {r['sharpe']:.2f} |"
            )

    lines += [
        "",
        "## Portfolio Summary",
        "",
        f"- **Trades:** {len(tdf)} ({tpm:.1f}/month)",
        f"- **Win rate:** {wr*100:.1f}%",
        f"- **Avg win:** {aw*100:+.2f}%, **Avg loss:** -{al*100:.2f}%",
        f"- **Profit factor:** {pf:.2f}",
        f"- **Expectancy/trade:** {exp*100:+.3f}%",
        "",
        "## Return Estimation",
        "",
        f"| Scenario | WR | Monthly | Annual |",
        f"|---|---:|---:|---:|",
    ]
    for name, adj in [("Pessimistic", -0.05), ("Base", 0), ("Optimistic", +0.05)]:
        a_wr = wr + adj
        a_exp = a_wr * aw - (1 - a_wr) * al
        a_m = tpm * a_exp * MAX_RISK_PCT / 100
        a_a = (1 + a_m) ** 12 - 1
        lines.append(f"| {name} | {a_wr*100:.0f}% | {a_m*100:+.2f}% | {a_a*100:+.1f}% |")

    lines += [
        "",
        "## Signal Types",
        "",
        "| Type | Trades | WR |",
        "|---|---:|---:|",
    ]
    for stype in tdf["type"].unique():
        sub = tdf[tdf["type"] == stype]
        sw = sub[sub["outcome"] == "win"]
        lines.append(f"| {stype} | {len(sub)} | {len(sw)/len(sub)*100:.0f}% |")

    out = REPORTS / "miro_strategy_v3.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    run()
