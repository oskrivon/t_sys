"""Icebreaker paper-trading daemon — the certified rule, run live (or replayed).

DEPLOYED RULE (fixed, ex-ante, no lookahead; see docs/ICEBREAKER_RESEARCH.md + memory
project_icebreaker_dataset): CLEAN(one_sided>=0.9 & spacing>60) AND mom-k0 >= 2.5% on a
respected multi-hour level break -> MARKET entry at the break close -> MANAGED exit
(stop buf0.1% behind the level, 50% off at +0.6%->BE, 0.4% trailing give-back, 30m horizon).

The validated exit is BAR-BASED (simulate_exit_bars walks 1m high/low), so managing live
positions on closed 1m bars is FAITHFUL to the backtest, not an approximation. The detector
is imported verbatim from scripts/research/icebreaker_major.py so detection cannot diverge.

Two modes:
  --replay <klines_dir>   replay 1m klines, assert the incremental exit reproduces the
                          research simulate_exit_bars per-setup (faithfulness gate), then
                          print pooled net for the deployed rule. NO network, NO orders.
  (default = live)        poll Bybit 1m klines for the universe each minute, open/manage
                          paper positions, append fills to the trade log. NO real orders.

    python scripts/run_icebreaker_paper.py --replay data/klines_3y
    python scripts/run_icebreaker_paper.py --universe data/klines_3y --log data/icebreaker_paper_trades.jsonl
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


major = _load("icebreaker_major", "scripts/research/icebreaker_major.py")

# --- frozen, identical to icebreaker_mom_gate_kl (the validated config) -------------------
BAR_MS = 60_000
MOM_WIN = 30
MOM_THR = 0.025
FEE_SIDE = 0.00055                       # VIP0 taker per side (paper baseline; logs gross too)
CFG = {"buffer": 0.001, "tp1": 0.006, "f1": 0.5, "trail_giveback": 0.004,
       "horizon_ms": 1_800_000}
DET = dict(lookback=480, swing_w=5, tol=0.0015, min_touches=4, min_span_bars=120,
           brk=0.0015, cooldown=30, near_bars=30, near_tol=0.003, one_sided=0.70)
WARMUP = DET["lookback"] + MOM_WIN + 10  # bars of history needed to detect on the last bar

# Certified 41-coin universe (scorecard 2023-06..2026-06; 1000SHIB dropped = empty/no edge).
DEFAULT_UNIVERSE = (
    "1000BONKUSDT 1000FLOKIUSDT 1000PEPEUSDT AAVEUSDT ADAUSDT APTUSDT ARBUSDT ATOMUSDT "
    "AVAXUSDT BCHUSDT BNBUSDT BTCUSDT DOGEUSDT DOTUSDT ENAUSDT ETCUSDT ETHUSDT FARTCOINUSDT "
    "FILUSDT INJUSDT JTOUSDT LDOUSDT LINKUSDT LTCUSDT NEARUSDT ONDOUSDT OPUSDT ORDIUSDT "
    "POPCATUSDT RUNEUSDT SEIUSDT SOLUSDT SUIUSDT TAOUSDT TIAUSDT TRXUSDT UNIUSDT WIFUSDT "
    "WLDUSDT XLMUSDT XRPUSDT").split()


def spacing(touches, span_bars):
    return span_bars / (touches - 1) if touches and touches > 1 and span_bars is not None else 0.0


def is_clean(bk):
    return bk.get("one_sided", 0) >= 0.9 and spacing(bk.get("touches"), bk.get("span_bars")) > 60


class Position:
    """Incremental mirror of icebreaker_mom_gate_kl.simulate_exit_bars. Feed it the entry
    bar first, then each subsequent closed bar via step(); it returns a result dict when the
    position closes (and on the horizon bar). Pessimistic intra-bar ordering (adverse first)."""

    def __init__(self, sym, ts0, side, level, mom, entry, cfg=CFG, fee_side=FEE_SIDE):
        self.sym, self.ts0, self.side, self.level, self.mom = sym, ts0, side, level, mom
        self.entry = entry
        self.cfg, self.fee_side = cfg, fee_side
        self.long = side == "long"
        self.dirn = 1.0 if self.long else -1.0
        self.stop0 = level * (1 - cfg["buffer"]) if self.long else level * (1 + cfg["buffer"])
        self.tp1_px = entry * (1 + self.dirn * cfg["tp1"])
        self.t_end = ts0 + cfg["horizon_ms"]
        self.legs, self.remaining, self.tp1_done = [], 1.0, False
        self.best = entry
        self.last = entry
        self.closed = False

    def _finish(self, reason):
        if self.remaining > 1e-9:                       # horizon: close remainder at last
            self.legs.append((self.remaining, self.last))
            self.remaining = 0.0
        gross = sum(g * self.dirn * (pp - self.entry) / self.entry for g, pp in self.legs)
        fee_units = 1.0 + sum(g for g, _ in self.legs)
        self.closed = True
        return {"sym": self.sym, "ts0": self.ts0, "side": self.side, "level": self.level,
                "mom": self.mom, "entry": self.entry, "gross": gross, "fee_units": fee_units,
                "net": gross - self.fee_side * fee_units, "reason": reason,
                "mfe": self.dirn * (self.best - self.entry) / self.entry}

    def step(self, bar):
        """Process one closed bar at/after ts0. Returns result dict if closed, else None."""
        if self.closed:
            return None
        if bar["ts"] > self.t_end:                      # past horizon -> close at last close
            return self._finish("horizon")
        hi, lo = bar["high"], bar["low"]
        self.last = bar["close"]
        adverse = lo if self.long else hi               # worst price for us in the bar
        favor = hi if self.long else lo                 # best price in the bar
        if self.tp1_done:
            trail = self.best * (1 - self.dirn * self.cfg["trail_giveback"])
            eff = max(trail, self.entry) if self.long else min(trail, self.entry)
            if self.dirn * (adverse - eff) <= 0:        # trail/BE stop hit
                self.legs.append((self.remaining, eff)); self.remaining = 0.0
                return self._finish("trail")
            if self.dirn * (favor - self.best) > 0:
                self.best = favor
        else:
            if self.dirn * (adverse - self.stop0) <= 0:  # hard stop first (pessimistic)
                self.legs.append((self.remaining, self.stop0)); self.remaining = 0.0
                return self._finish("stop")
            if self.dirn * (favor - self.tp1_px) >= 0:   # TP1 reached -> scale 50%, arm trail
                self.legs.append((self.cfg["f1"], self.tp1_px))
                self.remaining -= self.cfg["f1"]; self.tp1_done = True; self.best = favor
        return None


def fresh_signal(bars, ts_index, last_ts):
    """Run the detector on the trailing window; return a setup dict iff a CLEAN break with
    mom-k0 >= thr fired on the just-closed bar (ts_close == last_ts + BAR_MS). No lookahead:
    only the closed bar and its history are used."""
    bks = major.detect_major_breakouts(bars, BAR_MS, **DET)
    want = last_ts + BAR_MS
    for bk in bks:
        if bk["ts_close"] != want or not is_clean(bk):
            continue
        ei = ts_index.get(bk["ts_close"])
        # mom-k0 = signed 30m close return ending at the bar whose close == break close
        bi = ts_index.get(last_ts)
        if bi is None or bi < MOM_WIN:
            return None
        cl = bars[bi]["close"]; cl0 = bars[bi - MOM_WIN]["close"]
        sign = 1.0 if bk["side"] == "long" else -1.0
        mom = sign * (cl / cl0 - 1.0)
        if mom < MOM_THR:
            return None
        return {"side": bk["side"], "level": bk["level"], "mom": mom,
                "touches": bk["touches"], "span_bars": bk["span_bars"],
                "one_sided": bk["one_sided"], "ts_close": bk["ts_close"]}
    return None


# ------------------------------- replay (faithfulness gate) -------------------------------

def replay(klines_dir):
    import numpy as np
    kdir = Path(klines_dir)
    gate = _load("icebreaker_mom_gate_kl", "scripts/research/icebreaker_mom_gate_kl.py")
    syms = sorted(p.stem for p in kdir.glob("*.jsonl"))
    all_nets, mism = [], 0
    n_setups = 0
    for sym in syms:
        bars = [json.loads(l) for l in open(kdir / f"{sym}.jsonl") if l.strip()]
        bars.sort(key=lambda b: b["ts"])
        if len(bars) < DET["lookback"] + MOM_WIN:
            continue
        ts_index = {b["ts"]: k for k, b in enumerate(bars)}
        bar_cl = [b["close"] for b in bars]
        bks = major.detect_major_breakouts(bars, BAR_MS, **DET)
        for bk in bks:
            ts0 = bk["ts_close"]; ei = ts_index.get(ts0)
            if ei is None or ei < MOM_WIN:
                continue
            sign = 1.0 if bk["side"] == "long" else -1.0
            mom = sign * (bar_cl[ei] / bar_cl[ei - MOM_WIN] - 1.0)
            # research reference
            ref = gate.simulate_exit_bars(bars, ts_index, ts0, bk["side"], bk["level"],
                                          CFG, FEE_SIDE)
            if ref is None:
                continue
            n_setups += 1
            # incremental position
            pos = Position(sym, ts0, bk["side"], bk["level"], mom, bars[ei]["open"])
            res = None
            j = ei
            while j < len(bars) and res is None:
                res = pos.step(bars[j]); j += 1
            if res is None:
                res = pos._finish("horizon")
            if abs(res["net"] - ref["net"]) > 1e-9:
                mism += 1
                if mism <= 5:
                    print(f"  MISMATCH {sym} ts0={ts0}: live {res['net']:.6f} vs ref {ref['net']:.6f}")
            # deployed rule selection
            if is_clean(bk) and mom >= MOM_THR:
                all_nets.append(res["net"])
    print(f"\nfaithfulness: {n_setups} setups, {mism} mismatches vs research simulate_exit_bars")
    if all_nets:
        a = np.array(all_nets)
        print(f"DEPLOYED RULE (CLEAN & mom>=2.5%, live exit logic): n={len(a)} "
              f"net={a.mean()*100:+.3f}% WR={(a>0).mean()*100:.0f}% sum={a.sum()*100:+.1f}%")
    print("OK — live exit logic reproduces the validated dump." if mism == 0
          else f"!! {mism} mismatches — DO NOT deploy until resolved.")
    return mism == 0


# ------------------------------------- live loop ------------------------------------------

def load_universe(universe_dir, symbols_file):
    if symbols_file:
        return sorted({l.strip() for l in open(symbols_file) if l.strip()
                       and not l.startswith("#")})
    if universe_dir and Path(universe_dir).is_dir():
        found = sorted(p.stem for p in Path(universe_dir).glob("*.jsonl"))
        if found:
            return found
    return sorted(DEFAULT_UNIVERSE)


def live(syms, log_path, state_path, poll_s=20, heartbeat_path=None):
    import ccxt  # sync client; ~40 light REST calls/min is well within limits
    import structlog
    log = structlog.get_logger()
    ex = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    ccxt_sym = {s: s.replace("USDT", "/USDT:USDT") for s in syms}
    buffers = {s: [] for s in syms}            # rolling list of closed 1m bars
    positions = {}                              # sym -> Position
    last_seen = {s: 0 for s in syms}

    # resume open positions
    if Path(state_path).exists():
        for s, d in json.load(open(state_path)).items():
            p = Position(s, d["ts0"], d["side"], d["level"], d["mom"], d["entry"])
            p.__dict__.update(d["st"]); positions[s] = p
        log.info("icebreaker_paper_resumed", open=len(positions))

    logf = open(log_path, "a")

    def save_state():
        st = {s: {"ts0": p.ts0, "side": p.side, "level": p.level, "mom": p.mom,
                  "entry": p.entry, "st": {k: getattr(p, k) for k in
                  ("legs", "remaining", "tp1_done", "best", "last")}}
              for s, p in positions.items()}
        json.dump(st, open(state_path, "w"))

    def save_heartbeat(cycle, scanned, errs, cycle_s):
        """Liveness beacon (observability only; nothing here feeds detection/exit). Written
        every cycle so external monitors can tell 'alive & scanning' from 'stuck/dead' even
        during quiet periods with no signals and no errors."""
        if not heartbeat_path:
            return
        hb = {"ts": int(time.time() * 1000),
              "time": datetime.now(timezone.utc).isoformat(),
              "cycle": cycle, "universe": len(syms), "scanned": scanned,
              "open": len(positions), "open_syms": sorted(positions),
              "errors": errs, "cycle_s": round(cycle_s, 1)}
        tmp = str(heartbeat_path) + ".tmp"
        json.dump(hb, open(tmp, "w"))
        Path(tmp).replace(heartbeat_path)

    def fetch_closed(sym, limit):
        # Bybit returns the still-forming bar last; drop it so we only ever see closed bars.
        o = ex.fetch_ohlcv(ccxt_sym[sym], "1m", limit=limit)
        bars = [{"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
                 "close": r[4], "volume": r[5]} for r in o]
        return bars[:-1] if bars else bars

    log.info("icebreaker_paper_started", universe=len(syms), log=str(log_path))
    cycle = 0
    while True:
        t0 = time.time()
        errs = 0
        for sym in syms:
            try:
                need = WARMUP if not buffers[sym] else 5
                new = fetch_closed(sym, max(need, 2))
                if not new:
                    continue
                buf = buffers[sym]
                # append only genuinely-new closed bars
                base = buf[-1]["ts"] if buf else -1
                for b in new:
                    if b["ts"] > base:
                        buf.append(b)
                if len(buf) > WARMUP + 60:
                    del buf[:-(WARMUP + 60)]
                newest = buf[-1]
                if newest["ts"] == last_seen[sym]:
                    continue
                last_seen[sym] = newest["ts"]
                ts_index = {b["ts"]: k for k, b in enumerate(buf)}

                # 1) manage an open position on the just-closed bar
                if sym in positions:
                    res = positions[sym].step(newest)
                    if res:
                        del positions[sym]
                        res["exit_ts"] = newest["ts"]
                        res["exit_time"] = datetime.fromtimestamp(newest["ts"] / 1000, timezone.utc).isoformat()
                        logf.write(json.dumps(res) + "\n"); logf.flush()
                        log.info("icebreaker_paper_exit", sym=sym, net=round(res["net"] * 100, 3),
                                 reason=res["reason"], mfe=round(res["mfe"] * 100, 2))
                        save_state()

                # 2) if flat, look for a fresh signal on the just-closed bar
                if sym not in positions and len(buf) >= DET["lookback"] + MOM_WIN:
                    sig = fresh_signal(buf, ts_index, newest["ts"])
                    if sig:
                        entry = newest["close"]            # market fill at break close
                        pos = Position(sym, sig["ts_close"], sig["side"], sig["level"],
                                       sig["mom"], entry)
                        positions[sym] = pos
                        ent = {"event": "entry", "sym": sym, "ts0": sig["ts_close"],
                               "entry_time": datetime.fromtimestamp(newest["ts"] / 1000, timezone.utc).isoformat(),
                               "side": sig["side"], "level": sig["level"], "entry": entry,
                               "mom": sig["mom"], "one_sided": sig["one_sided"],
                               "spacing": round(spacing(sig["touches"], sig["span_bars"]), 1)}
                        logf.write(json.dumps(ent) + "\n"); logf.flush()
                        log.info("icebreaker_paper_entry", sym=sym, side=sig["side"],
                                 mom=round(sig["mom"] * 100, 2), entry=entry)
                        save_state()
            except Exception as e:
                errs += 1
                log.warning("icebreaker_paper_sym_error", sym=sym, error=str(e))
        cycle += 1
        cycle_s = time.time() - t0
        save_heartbeat(cycle, len(syms) - errs, errs, cycle_s)
        if cycle % 45 == 0:            # ~ every 15 min at poll_s=20 → positive liveness in the log
            log.info("icebreaker_paper_heartbeat", cycle=cycle, open=len(positions),
                     open_syms=sorted(positions), errors_last=errs)
        time.sleep(max(0, poll_s - cycle_s))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", help="klines dir to replay + verify faithfulness")
    ap.add_argument("--universe", default="data/klines_3y", help="dir of <SYM>.jsonl = live universe")
    ap.add_argument("--symbols-file", default="", help="txt of symbols (one/line); overrides --universe")
    ap.add_argument("--log", default="data/icebreaker_paper_trades.jsonl")
    ap.add_argument("--state", default="data/icebreaker_paper_state.json")
    ap.add_argument("--heartbeat", default="data/icebreaker_paper_heartbeat.json",
                    help="liveness beacon json (rewritten each cycle for external monitoring)")
    ap.add_argument("--poll-s", type=int, default=20)
    args = ap.parse_args()
    if args.replay:
        ok = replay(args.replay)
        sys.exit(0 if ok else 1)
    syms = load_universe(args.universe, args.symbols_file)
    live(syms, args.log, args.state, args.poll_s, args.heartbeat)


if __name__ == "__main__":
    main()
