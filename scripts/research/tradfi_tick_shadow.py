"""TradFi shadow: do tick bar anomalies appear when crypto catches up to equity moves?

Hypothesis: when NQ makes a big daily move, crypto eventually follows.
The "informed" flow in crypto is not insiders — it's funds/arb desks
reacting to equity moves. Tick bars should show:
  1. Sell% shift in NQ direction before crypto price fully catches up
  2. Duration compression (activity spikes) at onset of catch-up
  3. Volume surge without proportional price move (absorption phase)

Approach:
  - Get NQ daily returns (yfinance)
  - Get BTC tick bars from Binance Vision aggTrades
  - On days with |NQ ret| > 1%: measure BTC tick bar features
    during US session (14:30-21:00 UTC) vs post-close (21:00-06:00 UTC)
  - Compare: do tick features during post-close predict BTC catch-up direction?

Usage:
    python scripts/research/tradfi_tick_shadow.py [--days 180]
"""
import csv
import io
import zipfile
from datetime import datetime, timedelta, timezone, date as dateclass
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

import numpy as np

ROOT = Path("/root/trading")
CACHE_DIR = ROOT / "data" / "cache" / "aggtrades"
BASE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades"

TICK_SIZE = 500
FEE = 0.08


def ensure_cached(symbol: str, date_str: str) -> Path | None:
    cache_path = CACHE_DIR / f"{symbol}-{date_str}.csv"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path
    url = f"{BASE_URL}/{symbol}USDT/{symbol}USDT-aggTrades-{date_str}.zip"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urlopen(req, timeout=30)
        data = resp.read()
    except (HTTPError, Exception):
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        csv_data = zf.read(zf.namelist()[0])
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(csv_data)
        return cache_path
    except Exception:
        return None


def tick_features_for_window(csv_path: Path, start_ms: int, end_ms: int,
                              tick_size: int = 500) -> dict | None:
    """Compute tick bar features for trades in time window."""
    trades = []
    with open(csv_path, "r") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 7:
                continue
            try:
                ts = int(row[5])
                if ts < start_ms:
                    continue
                if ts >= end_ms:
                    break
                trades.append((ts, float(row[1]), float(row[2]),
                               row[6].strip().lower() == "true"))
            except (ValueError, IndexError):
                continue

    if len(trades) < tick_size * 3:
        return None

    # Build bars
    bars = []
    for i in range(0, len(trades) - tick_size + 1, tick_size):
        chunk = trades[i:i + tick_size]
        prices = [t[1] for t in chunk]
        qty = sum(t[2] for t in chunk)
        sell_qty = sum(t[2] for t in chunk if t[3])
        bars.append({
            "sell_pct": sell_qty / qty * 100 if qty > 0 else 50,
            "duration_s": (chunk[-1][0] - chunk[0][0]) / 1000,
            "ret": (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] > 0 else 0,
            "volume": qty,
        })

    if not bars:
        return None

    sell_pcts = [b["sell_pct"] for b in bars]
    durations = [b["duration_s"] for b in bars]
    rets = [b["ret"] for b in bars]

    # Price change over entire window
    total_ret = (trades[-1][1] - trades[0][1]) / trades[0][1] * 100

    return {
        "n_bars": len(bars),
        "n_trades": len(trades),
        "total_ret": total_ret,
        "avg_sell_pct": np.mean(sell_pcts),
        "sell_pct_trend": np.mean(sell_pcts[-3:]) - np.mean(sell_pcts[:3]) if len(sell_pcts) >= 6 else 0,
        "avg_duration_s": np.mean(durations),
        "min_duration_s": min(durations),
        "max_abs_bar_ret": max(abs(r) for r in rets),
        "net_sell_pressure": np.mean(sell_pcts) - 50,
        "volume_total": sum(b["volume"] for b in bars),
        # Late-window features (last 1/3 of bars)
        "late_sell_pct": np.mean(sell_pcts[-(len(sell_pcts)//3):]) if len(sell_pcts) >= 3 else 50,
        "late_ret": sum(rets[-(len(rets)//3):]) if len(rets) >= 3 else 0,
    }


def get_btc_price_at(csv_path: Path, ts_ms: int) -> float | None:
    """Get BTC price closest to timestamp."""
    with open(csv_path, "r") as f:
        reader = csv.reader(f)
        last_price = None
        for row in reader:
            if len(row) < 7:
                continue
            try:
                ts = int(row[5])
                price = float(row[1])
                if ts >= ts_ms:
                    return price
                last_price = price
            except (ValueError, IndexError):
                continue
    return last_price


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    args = parser.parse_args()
    days = args.days

    print("=" * 70)
    print("  TRADFI SHADOW: TICK BAR FEATURES + EQUITY LAG")
    print("=" * 70)
    print(f"  Days: {days}")
    print(f"  NQ threshold: |daily ret| > 1%")
    print(f"  Windows: US session (14:30-21:00), Post-close (21:00-06:00)\n")

    # Get NQ daily data
    import yfinance as yf
    print("  Fetching NQ data...", flush=True)
    nq = yf.download("QQQ", period=f"{days + 30}d", interval="1d", progress=False)
    if nq.empty:
        print("  No NQ data!")
        return

    nq = nq.reset_index()
    if hasattr(nq.columns, 'levels'):
        nq.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in nq.columns]
    else:
        nq.columns = [c.lower() for c in nq.columns]

    nq["ret"] = nq["close"].pct_change() * 100
    nq["date_val"] = nq["date"].dt.date if hasattr(nq["date"].dt, "date") else nq["date"]

    # Filter big NQ days
    big_nq = nq[nq["ret"].abs() > 1.0].copy()
    print(f"  NQ big days (|ret|>1%): {len(big_nq)}", flush=True)

    end_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=days)

    events = []

    for _, row in big_nq.iterrows():
        nq_date = row["date_val"]
        if hasattr(nq_date, 'item'):
            nq_date = nq_date.item()
        if isinstance(nq_date, datetime):
            nq_date = nq_date.date()

        if nq_date < start_date or nq_date > end_date:
            continue

        nq_ret = float(row["ret"])
        nq_dir = "up" if nq_ret > 0 else "down"
        date_str = nq_date.strftime("%Y-%m-%d")
        next_date_str = (nq_date + timedelta(days=1)).strftime("%Y-%m-%d")

        # US session: 14:30 - 21:00 UTC
        us_open_ms = int(datetime(nq_date.year, nq_date.month, nq_date.day,
                                   14, 30, tzinfo=timezone.utc).timestamp() * 1000)
        us_close_ms = int(datetime(nq_date.year, nq_date.month, nq_date.day,
                                    21, 0, tzinfo=timezone.utc).timestamp() * 1000)

        # Post-close: 21:00 - 06:00 next day
        next_day = nq_date + timedelta(days=1)
        post_start_ms = us_close_ms
        post_end_ms = int(datetime(next_day.year, next_day.month, next_day.day,
                                    6, 0, tzinfo=timezone.utc).timestamp() * 1000)

        # Outcome: BTC from US close to next day 12:00 UTC
        outcome_end_ms = int(datetime(next_day.year, next_day.month, next_day.day,
                                       12, 0, tzinfo=timezone.utc).timestamp() * 1000)

        # Get BTC tick features for both windows
        csv_today = ensure_cached("BTC", date_str)
        csv_next = ensure_cached("BTC", next_date_str)

        if csv_today is None:
            continue

        us_feat = tick_features_for_window(csv_today, us_open_ms, us_close_ms, TICK_SIZE)
        post_feat = tick_features_for_window(csv_today, post_start_ms, post_end_ms, TICK_SIZE)
        if post_feat is None and csv_next:
            # Post-close spans midnight
            post_feat = tick_features_for_window(csv_next, post_start_ms, post_end_ms, TICK_SIZE)

        # BTC return post-close → next morning
        btc_post_ret = None
        if csv_next:
            p_close = get_btc_price_at(csv_today, us_close_ms)
            p_morning = get_btc_price_at(csv_next, outcome_end_ms)
            if p_close and p_morning:
                btc_post_ret = (p_morning - p_close) / p_close * 100

        # BTC during US session
        btc_us_ret = us_feat["total_ret"] if us_feat else None

        # Sync ratio: how much BTC moved with NQ during US
        sync = abs(btc_us_ret / nq_ret) if btc_us_ret is not None and nq_ret != 0 else None

        events.append({
            "date": date_str,
            "nq_ret": nq_ret,
            "nq_dir": nq_dir,
            "btc_us_ret": btc_us_ret,
            "btc_post_ret": btc_post_ret,
            "sync_ratio": sync,
            "us_feat": us_feat,
            "post_feat": post_feat,
        })

    print(f"\n  Events with data: {len(events)}\n")

    if not events:
        return

    # ======================================================================
    # Analysis
    # ======================================================================

    print("=" * 70)
    print("  RESULTS")
    print("=" * 70)

    # 1. Basic: does BTC catch up post-close?
    has_post = [e for e in events if e["btc_post_ret"] is not None]
    print(f"\n  1. BTC CATCH-UP POST-CLOSE (NQ big days)\n")
    print(f"  {'NQ dir':<8s} {'N':>3s} {'BTC follows':>12s} {'Avg BTC post':>13s} {'Avg NQ':>8s}")
    print("  " + "-" * 50)

    for d in ["up", "down"]:
        group = [e for e in has_post if e["nq_dir"] == d]
        if not group:
            continue
        follows = sum(1 for e in group
                      if (d == "up" and e["btc_post_ret"] > 0) or
                         (d == "down" and e["btc_post_ret"] < 0))
        avg_post = np.mean([e["btc_post_ret"] for e in group])
        avg_nq = np.mean([e["nq_ret"] for e in group])
        print(f"  {d:<8s} {len(group):>3d} {follows}/{len(group)} = {follows/len(group)*100:.0f}%"
              f"   {avg_post:>+8.3f}%   {avg_nq:>+7.2f}%")

    # Total follow rate
    total_follows = sum(1 for e in has_post
                        if (e["nq_dir"] == "up" and e["btc_post_ret"] > 0) or
                           (e["nq_dir"] == "down" and e["btc_post_ret"] < 0))
    print(f"\n  Overall follow rate: {total_follows}/{len(has_post)} = "
          f"{total_follows/len(has_post)*100:.0f}%")

    # 2. Does sync ratio matter?
    has_sync = [e for e in has_post if e["sync_ratio"] is not None]
    if has_sync:
        print(f"\n\n  2. SYNC RATIO FILTER (BTC/NQ during US session)\n")
        print(f"  {'Filter':<25s} {'N':>3s} {'Follow%':>8s} {'Avg post':>9s} {'Edge':>6s}")
        print("  " + "-" * 55)

        for label, lo, hi in [
            ("Low sync (<0.3)", 0, 0.3),
            ("Mid sync (0.3-0.7)", 0.3, 0.7),
            ("High sync (>0.7)", 0.7, 999),
            ("Desync (<0.5)", 0, 0.5),
        ]:
            group = [e for e in has_sync if lo <= e["sync_ratio"] < hi]
            if not group:
                continue
            follows = sum(1 for e in group
                          if (e["nq_dir"] == "up" and e["btc_post_ret"] > 0) or
                             (e["nq_dir"] == "down" and e["btc_post_ret"] < 0))
            avg_post = np.mean([e["btc_post_ret"] for e in group])
            # Edge: trade in NQ direction, what's the PnL?
            pnls = []
            for e in group:
                if e["nq_dir"] == "up":
                    pnls.append(e["btc_post_ret"])
                else:
                    pnls.append(-e["btc_post_ret"])
            avg_pnl = np.mean(pnls)
            print(f"  {label:<25s} {len(group):>3d} {follows/len(group)*100:>7.0f}% "
                  f"{avg_post:>+8.3f}% {avg_pnl:>+5.3f}%")

    # 3. Tick bar features as filters
    has_post_feat = [e for e in has_post if e["post_feat"] is not None]
    if has_post_feat:
        print(f"\n\n  3. TICK BAR FEATURES POST-CLOSE AS SIGNAL\n")
        print(f"  Can tick bar sell% in post-close predict BTC catch-up direction?\n")

        # After NQ up day: is post-close sell% < 50 (buying) a signal?
        for nq_d, tick_signal, tick_label in [
            ("up", lambda e: e["post_feat"]["avg_sell_pct"] < 45, "post sell%<45 (buying)"),
            ("up", lambda e: e["post_feat"]["avg_sell_pct"] > 55, "post sell%>55 (selling)"),
            ("down", lambda e: e["post_feat"]["avg_sell_pct"] > 55, "post sell%>55 (selling)"),
            ("down", lambda e: e["post_feat"]["avg_sell_pct"] < 45, "post sell%<45 (buying)"),
            ("up", lambda e: e["post_feat"]["late_sell_pct"] < 45, "late sell%<45"),
            ("down", lambda e: e["post_feat"]["late_sell_pct"] > 55, "late sell%>55"),
        ]:
            group = [e for e in has_post_feat if e["nq_dir"] == nq_d and tick_signal(e)]
            if not group:
                continue
            follows = sum(1 for e in group
                          if (nq_d == "up" and e["btc_post_ret"] > 0) or
                             (nq_d == "down" and e["btc_post_ret"] < 0))
            pnls = [e["btc_post_ret"] if nq_d == "up" else -e["btc_post_ret"] for e in group]
            avg_pnl = np.mean(pnls) - FEE
            print(f"  NQ {nq_d:>4s} + {tick_label:<25s}: N={len(group):>2d} "
                  f"follow={follows/len(group)*100:.0f}% net={avg_pnl:+.3f}%")

    # 4. Combined: desync + tick direction
    print(f"\n\n  4. COMBINED: DESYNC + TICK DIRECTION\n")
    print(f"  Enter in NQ direction when: BTC desynced + tick bars confirm\n")

    combined = [e for e in has_post_feat if e["sync_ratio"] is not None and e["sync_ratio"] < 0.5]
    if combined:
        for label, cond in [
            ("Desync only (no tick filter)",
             lambda e: True),
            ("Desync + tick aligns with NQ",
             lambda e: (e["nq_dir"] == "up" and e["post_feat"]["avg_sell_pct"] < 48) or
                       (e["nq_dir"] == "down" and e["post_feat"]["avg_sell_pct"] > 52)),
            ("Desync + late tick aligns",
             lambda e: (e["nq_dir"] == "up" and e["post_feat"]["late_sell_pct"] < 45) or
                       (e["nq_dir"] == "down" and e["post_feat"]["late_sell_pct"] > 55)),
            ("Desync + tick OPPOSES NQ (contrarian)",
             lambda e: (e["nq_dir"] == "up" and e["post_feat"]["avg_sell_pct"] > 55) or
                       (e["nq_dir"] == "down" and e["post_feat"]["avg_sell_pct"] < 45)),
        ]:
            filtered = [e for e in combined if cond(e)]
            if not filtered:
                continue
            pnls = []
            for e in filtered:
                if e["nq_dir"] == "up":
                    pnls.append(e["btc_post_ret"])
                else:
                    pnls.append(-e["btc_post_ret"])
            pnls = np.array(pnls)
            net = pnls - FEE
            wr = (pnls > 0).mean() * 100
            sh = net.mean() / pnls.std() if pnls.std() > 0 else 0
            print(f"  {label:<40s}: N={len(filtered):>2d} WR={wr:.0f}% "
                  f"avg={net.mean():+.3f}% sharpe/trade={sh:.2f}")

    # Event detail table
    print(f"\n\n  EVENT DETAIL:\n")
    print(f"  {'Date':<12s} {'NQ':>7s} {'BTC US':>8s} {'Sync':>6s} {'BTC post':>9s} "
          f"{'PostSell%':>9s} {'Follow':>7s}")
    print("  " + "-" * 65)
    for e in sorted(events, key=lambda x: x["date"]):
        sync = f"{e['sync_ratio']:.2f}" if e["sync_ratio"] is not None else "?"
        btc_us = f"{e['btc_us_ret']:+.2f}%" if e["btc_us_ret"] is not None else "?"
        btc_post = f"{e['btc_post_ret']:+.2f}%" if e["btc_post_ret"] is not None else "?"
        post_sell = ""
        if e["post_feat"]:
            post_sell = f"{e['post_feat']['avg_sell_pct']:.0f}%"
        followed = ""
        if e["btc_post_ret"] is not None:
            f_ = (e["nq_dir"] == "up" and e["btc_post_ret"] > 0) or \
                 (e["nq_dir"] == "down" and e["btc_post_ret"] < 0)
            followed = "YES" if f_ else "no"
        print(f"  {e['date']:<12s} {e['nq_ret']:>+6.2f}% {btc_us:>8s} {sync:>6s} "
              f"{btc_post:>9s} {post_sell:>9s} {followed:>7s}")


if __name__ == "__main__":
    main()
