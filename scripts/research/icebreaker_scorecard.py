"""Scorecard validation of the icebreaker CLEAN x mom-k0 fixed-threshold rule, run through
the project's standard pipeline (CPCV / DSR / MinBTL / PBO / regime) -- apples-to-apples
with Weekend/VR/Miro.

Rule under test (fixed, ex-ante, no lookahead): CLEAN(one_sided>=0.9 & spacing>60) AND
mom-k0 >= thr -> market entry at break close -> managed exit. Any coin.

net is taken from the klines bar-exit dump, with a flat HAIRCUT for klines optimism (~+0.05pp
vs tape, measured on the Feb-May overlap) so the validated edge is honest. PBO uses the
mom-threshold grid as the variant axis (does the in-sample-best threshold hold OOS?).

    python scripts/research/icebreaker_scorecard.py --dump data/ib_momdump_wide.jsonl \
        --klines data/klines_wide --mom-thr 0.02 --haircut 0.0005 --n-trials 25
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.backtest.models import ExitReason, Side, Trade  # noqa: E402
from src.validation.scorecard import validate_strategy  # noqa: E402

THR_GRID = [0.006, 0.010, 0.015, 0.020, 0.025, 0.030]


def build_trades(rows, thr, haircut):
    out = []
    for r in rows:
        if not r["clean"] or r["mom"] < thr:
            continue
        net = r["net_trail"] - haircut
        t0 = datetime.fromtimestamp(r["ts"] / 1000, timezone.utc)
        out.append(Trade(symbol=r["sym"], side=Side.LONG, entry_time=t0,
                         exit_time=t0 + timedelta(minutes=15),
                         entry_price=1.0, exit_price=1.0 + net, size_usd=10_000.0,
                         exit_reason=ExitReason.SIGNAL))
    return out


def daily_series(rows, thr, haircut, idx):
    by_day = {}
    for r in rows:
        if not r["clean"] or r["mom"] < thr:
            continue
        d = pd.Timestamp(datetime.fromtimestamp(r["ts"] / 1000, timezone.utc).date(), tz="UTC")
        by_day[d] = by_day.get(d, 0.0) + (r["net_trail"] - haircut)
    s = pd.Series(0.0, index=idx)
    for d, v in by_day.items():
        if d in s.index:
            s[d] = v
    return s.values


def btc_daily_returns(klines_dir):
    p = Path(klines_dir) / "BTCUSDT.jsonl"
    if not p.exists():
        return None
    rows = [json.loads(l) for l in open(p) if l.strip()]
    df = pd.DataFrame(rows)
    df["day"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.floor("D")
    daily_close = df.groupby("day")["close"].last()
    return daily_close.pct_change().dropna()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--klines", default="")
    ap.add_argument("--mom-thr", type=float, default=0.02)
    ap.add_argument("--haircut", type=float, default=0.0005)
    ap.add_argument("--n-trials", type=int, default=25)
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.dump) if l.strip()]
    trades = build_trades(rows, args.mom_thr, args.haircut)
    nets = [t.net_pnl_pct for t in trades]
    print(f"RULE: CLEAN & mom-k0 >= {args.mom_thr:.1%}  (haircut {args.haircut*1e4:.0f}bps for klines optimism)")
    print(f"trades={len(trades)}  mean net={np.mean(nets)*100:+.3f}%  "
          f"WR={np.mean([n>0 for n in nets])*100:.0f}%  "
          f"sum={np.sum(nets)*100:+.1f}%  coins={len({t.symbol for t in trades})}")
    if len(trades) < 30:
        print("too few trades"); return

    # PBO variant matrix over the mom-threshold grid
    idx = pd.date_range(
        min(t.entry_time for t in trades).date(),
        max(t.entry_time for t in trades).date(), freq="D", tz="UTC")
    cols = [daily_series(rows, v, args.haircut, idx) for v in THR_GRID]
    returns_matrix = np.column_stack(cols)

    btc = btc_daily_returns(args.klines) if args.klines else None

    report = validate_strategy(
        trades, n_trials=args.n_trials,
        strategy_name=f"icebreaker CLEAN x mom>={args.mom_thr:.1%}",
        returns_matrix=returns_matrix, btc_returns=btc)
    report.print_scorecard()


if __name__ == "__main__":
    main()
