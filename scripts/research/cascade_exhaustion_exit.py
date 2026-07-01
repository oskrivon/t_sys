"""Cascade tick bars: exhaustion-based exit experiment.

Same trigger as cascade_tick_bars.py (|ret|>0.5%, vol>3x on tick bars),
but instead of fixed hold or dumb trailing stop, detect move exhaustion:

Exhaustion signals (after entry in cascade direction):
  1. Duration expansion: bar takes >2x the trigger bar's duration
  2. Imbalance flip: sell% crosses 50% threshold (for short cascade)
  3. Momentum fade: 2 consecutive bars with shrinking |return|
  4. Combined: any 2 of the above

Also test: sell% > 80% as entry filter (from previous research).
"""
import ccxt
import time
import numpy as np
from datetime import datetime, timezone, timedelta

binance = ccxt.binanceusdm({"enableRateLimit": True})
binance.load_markets()

SYMBOLS = ["ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT",
           "SUI/USDT:USDT", "AVAX/USDT:USDT"]

TICK_SIZE = 500
DAYS = 3
COOLDOWN = 20
FEE = 0.08
MAX_HOLD = 20  # max bars to hold before forced exit


def fetch_agg_trades(symbol: str, since_ms: int, limit_trades: int = 200_000) -> list[dict]:
    market = binance.market(symbol)
    binance_sym = market["id"]
    trades = []
    params = {"startTime": since_ms, "limit": 1000}
    while len(trades) < limit_trades:
        try:
            raw = binance.fapiPublicGetAggTrades({"symbol": binance_sym, **params})
        except Exception as e:
            print(f"  Error: {e}")
            break
        if not raw:
            break
        for t in raw:
            trades.append({
                "id": int(t["a"]),
                "ts": int(t["T"]),
                "price": float(t["p"]),
                "qty": float(t["q"]),
                "is_sell": t["m"],
            })
        last_id = int(raw[-1]["a"])
        params = {"fromId": last_id + 1, "limit": 1000}
        if len(raw) < 1000:
            break
        time.sleep(0.1)
    return trades


def build_tick_bars(trades: list[dict], tick_size: int) -> list[dict]:
    bars = []
    for i in range(0, len(trades) - tick_size + 1, tick_size):
        chunk = trades[i:i + tick_size]
        prices = [t["price"] for t in chunk]
        qty = sum(t["qty"] for t in chunk)
        sell_qty = sum(t["qty"] for t in chunk if t["is_sell"])
        bars.append({
            "ts": chunk[0]["ts"],
            "open": prices[0],
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
            "volume": qty,
            "n_trades": tick_size,
            "sell_pct": sell_qty / qty * 100 if qty > 0 else 50,
            "duration_s": (chunk[-1]["ts"] - chunk[0]["ts"]) / 1000,
        })
    return bars


def compute_bar_features(bars: list[dict]):
    """Add derived features to bars."""
    for i in range(1, len(bars)):
        bars[i]["ret"] = (bars[i]["close"] - bars[i - 1]["close"]) / bars[i - 1]["close"] * 100
    # running avg duration (last 10 bars)
    for i in range(10, len(bars)):
        bars[i]["avg_dur_10"] = np.mean([b["duration_s"] for b in bars[i-10:i]])


# ======================================================================
# Exit strategies
# ======================================================================

def exit_fixed_hold(bars, entry_idx, direction, hold_n):
    """Baseline: fixed hold N bars."""
    idx = min(entry_idx + hold_n, len(bars) - 1)
    return idx


def exit_trailing(bars, entry_idx, direction, trail_pct=0.3):
    """Baseline: trailing stop from peak."""
    entry = bars[entry_idx]["close"]
    best = 0
    for j in range(entry_idx + 1, min(entry_idx + MAX_HOLD, len(bars))):
        if direction > 0:
            move = (bars[j]["high"] - entry) / entry * 100
            current = (bars[j]["close"] - entry) / entry * 100
        else:
            move = (entry - bars[j]["low"]) / entry * 100
            current = (entry - bars[j]["close"]) / entry * 100
        if move > best:
            best = move
        if best > trail_pct and best - current > trail_pct:
            return j
    return min(entry_idx + MAX_HOLD, len(bars) - 1)


def exit_duration_expansion(bars, entry_idx, direction, mult=2.0):
    """Exit when bar duration > mult * trigger bar duration."""
    trigger_dur = max(bars[entry_idx]["duration_s"], 1)
    for j in range(entry_idx + 1, min(entry_idx + MAX_HOLD, len(bars))):
        if bars[j]["duration_s"] > trigger_dur * mult:
            return j
    return min(entry_idx + MAX_HOLD, len(bars) - 1)


def exit_imbalance_flip(bars, entry_idx, direction):
    """Exit when sell/buy pressure flips against cascade direction."""
    for j in range(entry_idx + 1, min(entry_idx + MAX_HOLD, len(bars))):
        if direction < 0:
            # Short cascade (selling) — exit when buying takes over
            if bars[j]["sell_pct"] < 40:
                return j
        else:
            # Long cascade — exit when selling takes over
            if bars[j]["sell_pct"] > 60:
                return j
    return min(entry_idx + MAX_HOLD, len(bars) - 1)


def exit_momentum_fade(bars, entry_idx, direction):
    """Exit after 2 consecutive bars with shrinking |return|."""
    prev_abs_ret = abs(bars[entry_idx].get("ret", 1))
    fade_count = 0
    for j in range(entry_idx + 1, min(entry_idx + MAX_HOLD, len(bars))):
        curr_abs_ret = abs(bars[j].get("ret", 0))
        if curr_abs_ret < prev_abs_ret * 0.5:
            fade_count += 1
            if fade_count >= 2:
                return j
        else:
            fade_count = 0
        prev_abs_ret = curr_abs_ret
    return min(entry_idx + MAX_HOLD, len(bars) - 1)


def exit_combined(bars, entry_idx, direction):
    """Exit when 2+ exhaustion signals fire on same bar."""
    trigger_dur = max(bars[entry_idx]["duration_s"], 1)
    prev_abs_ret = abs(bars[entry_idx].get("ret", 1))
    fade_count = 0

    for j in range(entry_idx + 1, min(entry_idx + MAX_HOLD, len(bars))):
        signals = 0

        # 1. Duration expansion
        if bars[j]["duration_s"] > trigger_dur * 2:
            signals += 1

        # 2. Imbalance flip
        if direction < 0 and bars[j]["sell_pct"] < 40:
            signals += 1
        elif direction > 0 and bars[j]["sell_pct"] > 60:
            signals += 1

        # 3. Momentum fade
        curr_abs_ret = abs(bars[j].get("ret", 0))
        if curr_abs_ret < prev_abs_ret * 0.5:
            fade_count += 1
        else:
            fade_count = 0
        if fade_count >= 2:
            signals += 1
        prev_abs_ret = curr_abs_ret

        if signals >= 2:
            return j

    return min(entry_idx + MAX_HOLD, len(bars) - 1)


def exit_any_exhaustion(bars, entry_idx, direction):
    """Exit on first exhaustion signal (most aggressive)."""
    trigger_dur = max(bars[entry_idx]["duration_s"], 1)
    prev_abs_ret = abs(bars[entry_idx].get("ret", 1))
    fade_count = 0

    for j in range(entry_idx + 1, min(entry_idx + MAX_HOLD, len(bars))):
        # Duration expansion
        if bars[j]["duration_s"] > trigger_dur * 2:
            return j
        # Imbalance flip
        if direction < 0 and bars[j]["sell_pct"] < 40:
            return j
        if direction > 0 and bars[j]["sell_pct"] > 60:
            return j
        # Momentum fade (2 consecutive)
        curr_abs_ret = abs(bars[j].get("ret", 0))
        if curr_abs_ret < prev_abs_ret * 0.5:
            fade_count += 1
            if fade_count >= 2:
                return j
        else:
            fade_count = 0
        prev_abs_ret = curr_abs_ret

    return min(entry_idx + MAX_HOLD, len(bars) - 1)


# ======================================================================
# Strategy runner
# ======================================================================

EXIT_STRATEGIES = {
    "hold_5":       lambda bars, i, d: exit_fixed_hold(bars, i, d, 5),
    "hold_10":      lambda bars, i, d: exit_fixed_hold(bars, i, d, 10),
    "trail_0.3%":   lambda bars, i, d: exit_trailing(bars, i, d, 0.3),
    "trail_0.5%":   lambda bars, i, d: exit_trailing(bars, i, d, 0.5),
    "dur_expand":   exit_duration_expansion,
    "imb_flip":     exit_imbalance_flip,
    "mom_fade":     exit_momentum_fade,
    "combined_2":   exit_combined,
    "any_exhaust":  exit_any_exhaustion,
}


def run_strategy(bars: list[dict], sell_filter: float = 0) -> dict[str, list]:
    """Run trigger detection with all exit strategies.
    sell_filter: min sell% for short triggers (0 = disabled).
    """
    if len(bars) < 30:
        return {k: [] for k in EXIT_STRATEGIES}

    compute_bar_features(bars)
    avg_vol = np.mean([b["volume"] for b in bars[20:]])

    results = {name: [] for name in EXIT_STRATEGIES}
    cooldown = 0

    for i in range(20, len(bars) - MAX_HOLD - 5):
        if cooldown > 0:
            cooldown -= 1
            continue

        b = bars[i]
        if "ret" not in b:
            continue

        if abs(b["ret"]) > 0.5 and b["volume"] > avg_vol * 3:
            direction = 1 if b["ret"] > 0 else -1

            # Optional sell% entry filter
            if sell_filter > 0:
                if direction < 0 and b["sell_pct"] < sell_filter:
                    continue
                if direction > 0 and (100 - b["sell_pct"]) < sell_filter:
                    continue

            entry = b["close"]
            dt = datetime.fromtimestamp(b["ts"] / 1000, tz=timezone.utc)

            for name, exit_fn in EXIT_STRATEGIES.items():
                exit_idx = exit_fn(bars, i, direction)
                exit_price = bars[exit_idx]["close"]
                pnl = direction * (exit_price - entry) / entry * 100
                hold_bars = exit_idx - i

                # MFE
                mfe = 0
                for j in range(i + 1, exit_idx + 1):
                    if direction > 0:
                        m = (bars[j]["high"] - entry) / entry * 100
                    else:
                        m = (entry - bars[j]["low"]) / entry * 100
                    mfe = max(mfe, m)

                results[name].append({
                    "date": dt.strftime("%m-%d %H:%M:%S"),
                    "trigger": b["ret"],
                    "sell_pct": b["sell_pct"],
                    "dur_s": b["duration_s"],
                    "direction": direction,
                    "pnl": pnl,
                    "mfe": mfe,
                    "hold_bars": hold_bars,
                    "capture": pnl / mfe * 100 if mfe > 0 else 0,
                })

            cooldown = COOLDOWN

    return results


def main():
    print("=" * 70)
    print("  CASCADE TICK BARS: EXHAUSTION EXIT EXPERIMENT")
    print("=" * 70)
    print(f"\n  Tick size: {TICK_SIZE},  Lookback: {DAYS}d,  Fee: {FEE}%")
    print(f"  Max hold: {MAX_HOLD} bars\n")

    since_ms = int((datetime.now(timezone.utc) - timedelta(days=DAYS)).timestamp() * 1000)

    # Aggregate results across coins
    agg_no_filter = {name: [] for name in EXIT_STRATEGIES}
    agg_with_filter = {name: [] for name in EXIT_STRATEGIES}

    for sym in SYMBOLS:
        coin = sym.split("/")[0]
        print(f"\n--- {coin} ---")

        print(f"  Fetching aggTrades...", end=" ", flush=True)
        raw_trades = fetch_agg_trades(sym, since_ms)
        print(f"{len(raw_trades):,} trades")

        if len(raw_trades) < TICK_SIZE * 30:
            print(f"  Skipping (too few)")
            continue

        bars = build_tick_bars(raw_trades, TICK_SIZE)
        print(f"  Tick bars: {len(bars)}")

        # Without sell% filter
        res = run_strategy(bars, sell_filter=0)
        for name in EXIT_STRATEGIES:
            for t in res[name]:
                t["sym"] = coin
            agg_no_filter[name].extend(res[name])

        # With sell% > 80% filter
        res_f = run_strategy(bars, sell_filter=80)
        for name in EXIT_STRATEGIES:
            for t in res_f[name]:
                t["sym"] = coin
            agg_with_filter[name].extend(res_f[name])

        # Quick per-coin summary
        n = len(res["hold_10"])
        nf = len(res_f["hold_10"])
        print(f"  Triggers: {n} (no filter), {nf} (sell%>80%)")

    # ======================================================================
    # Results
    # ======================================================================

    for filter_name, agg in [("NO FILTER", agg_no_filter),
                              ("SELL% > 80% ENTRY FILTER", agg_with_filter)]:
        print(f"\n\n{'='*70}")
        print(f"  EXIT STRATEGY COMPARISON — {filter_name}")
        print(f"{'='*70}\n")

        print(f"  {'Strategy':<15s} {'N':>3s} {'WR':>5s} {'Avg':>8s} {'Net':>8s} "
              f"{'MFE':>7s} {'Capt':>6s} {'Hold':>5s} {'Sharpe':>7s}")
        print("  " + "-" * 65)

        for name in EXIT_STRATEGIES:
            trades = agg[name]
            if not trades:
                print(f"  {name:<15s}   0     -        -        -       -      -     -       -")
                continue

            pnls = np.array([t["pnl"] for t in trades])
            mfes = np.array([t["mfe"] for t in trades])
            captures = np.array([t["capture"] for t in trades])
            holds = np.array([t["hold_bars"] for t in trades])
            net = pnls - FEE
            wr = (pnls > 0).mean() * 100
            sh = net.mean() / pnls.std() if pnls.std() > 0 else 0

            print(f"  {name:<15s} {len(trades):>3d} {wr:>4.0f}% {pnls.mean():>+7.3f}% "
                  f"{net.mean():>+7.3f}% {mfes.mean():>+6.3f}% {captures.mean():>5.0f}% "
                  f"{holds.mean():>4.1f} {sh:>7.2f}")

    # Detail: best strategy trades
    # Find strategy with best net
    best_name = max(EXIT_STRATEGIES.keys(),
                    key=lambda n: np.mean([t["pnl"] for t in agg_no_filter[n]]) - FEE
                    if agg_no_filter[n] else -999)
    best_trades = agg_no_filter[best_name]

    if best_trades:
        print(f"\n\n--- BEST STRATEGY: {best_name} — TRADE DETAIL ---\n")
        print(f"  {'Coin':>5s} {'Date':>14s} {'Dir':>5s} {'Trig':>7s} {'Sell%':>6s} "
              f"{'PnL':>8s} {'MFE':>7s} {'Capt':>6s} {'Hold':>5s}")
        print("  " + "-" * 70)
        for t in sorted(best_trades, key=lambda x: x["pnl"], reverse=True):
            d = "LONG" if t["direction"] > 0 else "SHORT"
            print(f"  {t['sym']:>5s} {t['date']:>14s} {d:>5s} {t['trigger']:>+6.1f}% "
                  f"{t['sell_pct']:>5.0f}% {t['pnl']:>+7.2f}% {t['mfe']:>+6.2f}% "
                  f"{t['capture']:>5.0f}% {t['hold_bars']:>4d}")

    # Same for filtered
    best_name_f = max(EXIT_STRATEGIES.keys(),
                      key=lambda n: np.mean([t["pnl"] for t in agg_with_filter[n]]) - FEE
                      if agg_with_filter[n] else -999)
    best_trades_f = agg_with_filter[best_name_f]

    if best_trades_f:
        print(f"\n\n--- BEST FILTERED STRATEGY: {best_name_f} — TRADE DETAIL ---\n")
        print(f"  {'Coin':>5s} {'Date':>14s} {'Dir':>5s} {'Trig':>7s} {'Sell%':>6s} "
              f"{'PnL':>8s} {'MFE':>7s} {'Capt':>6s} {'Hold':>5s}")
        print("  " + "-" * 70)
        for t in sorted(best_trades_f, key=lambda x: x["pnl"], reverse=True):
            d = "LONG" if t["direction"] > 0 else "SHORT"
            print(f"  {t['sym']:>5s} {t['date']:>14s} {d:>5s} {t['trigger']:>+6.1f}% "
                  f"{t['sell_pct']:>5.0f}% {t['pnl']:>+7.2f}% {t['mfe']:>+6.2f}% "
                  f"{t['capture']:>5.0f}% {t['hold_bars']:>4d}")


if __name__ == "__main__":
    main()
