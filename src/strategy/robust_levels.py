"""Robust price-level + squeeze detection (reusable, pure functions).

Synthesizes established techniques (see docs/RESEARCH.md sources) into one place:

  * causal fractal pivots           (Bill Williams fractals)
  * ATR-scaled proximity clustering  (POC-clustering style, not fixed %)
  * volume profile / POC             (levels live where volume traded — HVN)
  * level quality score              (touches × bounce × recency × volume-at-level)
  * TTM squeeze (поджатие)           (Bollinger Bands inside Keltner Channels)
  * base one-sidedness               (edge-of-structure vs mid-range, measured on
                                      the LOCAL base, not stale history)

Everything operates on plain numpy OHLCV arrays so it works on pandas frames and
on our bar-dict lists alike. No I/O. Designed for causal (no-look-ahead) use:
every function reads only bars up to the evaluation index.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


# ----------------------------------------------------------------------------
# Volatility / bands
# ----------------------------------------------------------------------------
def true_range(high, low, close):
    """Wilder true range; tr[0] = high-low."""
    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    return np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))


def atr(high, low, close, period=14):
    """Rolling-mean ATR (simple, causal). Returns array aligned to input."""
    tr = true_range(high, low, close)
    out = np.full_like(tr, np.nan)
    if len(tr) >= period:
        c = np.cumsum(tr)
        out[period - 1] = c[period - 1] / period
        out[period:] = (c[period:] - c[:-period]) / period
    return out


def rolling(arr, period, fn):
    out = np.full(len(arr), np.nan)
    for i in range(period - 1, len(arr)):
        out[i] = fn(arr[i - period + 1:i + 1])
    return out


def ttm_squeeze(high, low, close, *, length=20, bb_mult=2.0, kc_mult=1.5):
    """Bool array: True where Bollinger Bands sit INSIDE Keltner Channels.

    The canonical coil/поджатие detector: low volatility compression that tends
    to precede an expansion (breakout)."""
    n = len(close)
    ma = rolling(close, length, np.mean)
    sd = rolling(close, length, lambda w: np.std(w))
    rng = atr(high, low, close, length)
    bb_up, bb_dn = ma + bb_mult * sd, ma - bb_mult * sd
    kc_up, kc_dn = ma + kc_mult * rng, ma - kc_mult * rng
    sq = np.zeros(n, dtype=bool)
    ok = ~np.isnan(bb_up) & ~np.isnan(kc_up)
    sq[ok] = (bb_up[ok] <= kc_up[ok]) & (bb_dn[ok] >= kc_dn[ok])
    return sq


def bandwidth_low(close, *, length=20, lookback=120):
    """Bool: 20-period Bollinger BandWidth at its lowest over `lookback` (squeeze)."""
    ma = rolling(close, length, np.mean)
    sd = rolling(close, length, lambda w: np.std(w))
    bw = np.where(ma > 0, 2 * bb_mult_safe(sd) / ma, np.nan)
    out = np.zeros(len(close), dtype=bool)
    for i in range(length + lookback, len(close)):
        w = bw[i - lookback:i + 1]
        if not np.isnan(bw[i]) and np.nanmin(w) == bw[i]:
            out[i] = True
    return out


def bb_mult_safe(sd):
    return 2.0 * sd


# ----------------------------------------------------------------------------
# Volume profile
# ----------------------------------------------------------------------------
def volume_profile(price, qty, *, lo=None, hi=None, bins=100):
    """Histogram of traded notional by price. Returns (centers, mass).

    Use trade-tape price/qty for a true profile, or bar close/volume as a proxy.
    POC = centers[mass.argmax()]; high-volume nodes = bins above a threshold.
    """
    price = np.asarray(price, dtype="float64")
    qty = np.asarray(qty, dtype="float64")
    if len(price) == 0:
        return np.empty(0), np.empty(0)
    lo = price.min() if lo is None else lo
    hi = price.max() if hi is None else hi
    if hi <= lo:
        return np.array([lo]), np.array([float((price * qty).sum())])
    edges = np.linspace(lo, hi, bins + 1)
    mass, _ = np.histogram(price, bins=edges, weights=price * qty)
    centers = (edges[:-1] + edges[1:]) / 2
    return centers, mass


def volume_at(centers, mass, level, half_width):
    """Fraction of total profile mass within ±half_width of `level`."""
    if len(centers) == 0 or mass.sum() <= 0:
        return 0.0
    sel = np.abs(centers - level) <= half_width
    return float(mass[sel].sum() / mass.sum())


# ----------------------------------------------------------------------------
# Pivots + clustering + scoring
# ----------------------------------------------------------------------------
def find_pivots(high, low, order=3):
    """Causal fractal pivots: (idx, price, 'high'|'low'). Strict extremum of [i-order,i]."""
    out = []
    for i in range(order, len(high)):
        if high[i] > max(high[i - order:i], default=-np.inf):
            out.append((i, float(high[i]), "high"))
        if low[i] < min(low[i - order:i], default=np.inf):
            out.append((i, float(low[i]), "low"))
    return out


def cluster_pivots(pivots, proximity):
    """Greedy 1-D clustering by absolute price proximity (pass ATR*mult).

    Returns list of dicts: price, members [(idx,price,kind)], n, last_idx, kind_mix."""
    if not pivots:
        return []
    pts = sorted(pivots, key=lambda p: p[1])
    clusters, cur = [], [pts[0]]
    for p in pts[1:]:
        ref = np.mean([x[1] for x in cur])
        if abs(p[1] - ref) <= proximity:
            cur.append(p)
        else:
            clusters.append(cur); cur = [p]
    clusters.append(cur)
    out = []
    for c in clusters:
        prices = [x[1] for x in c]
        out.append({"price": float(np.mean(prices)), "members": c, "n": len(c),
                    "last_idx": max(x[0] for x in c),
                    "zone_low": min(prices), "zone_high": max(prices)})
    return out


def score_level(cluster, high, low, close, eval_idx, centers, mass, atr_val):
    """Composite quality score for a level as of eval_idx (causal).

    Combines: touches, recency, bounce strength (avg reversal after each touch),
    tightness, and volume-at-level. Returns (score, detail dict)."""
    L = cluster["price"]
    n = cluster["n"]
    # recency: 1.0 if touched recently, decays over ~lookback
    age = eval_idx - cluster["last_idx"]
    recency = float(np.exp(-age / 240.0))
    # bounce: for each touch, the max move away from L within 20 bars after (causal)
    bounces = []
    for idx, px, kind in cluster["members"]:
        j0, j1 = idx + 1, min(idx + 21, eval_idx)
        if j1 <= j0:
            continue
        if kind == "high":
            bounces.append((px - low[j0:j1].min()) / px)
        else:
            bounces.append((high[j0:j1].max() - px) / px)
    bounce = float(np.mean(bounces)) if bounces else 0.0
    tight = (cluster["zone_high"] - cluster["zone_low"]) / L
    tightness = float(np.exp(-tight / 0.004))               # tighter cluster = better
    vol = volume_at(centers, mass, L, half_width=max(atr_val, L * 0.001))
    score = (n * 1.0) * recency * (1 + 4 * bounce) * (0.5 + tightness) * (1 + 6 * vol)
    return float(score), {"touches": n, "recency": round(recency, 2),
                          "bounce": round(bounce, 4), "vol_at_level": round(vol, 3),
                          "tightness": round(tightness, 2)}


# ----------------------------------------------------------------------------
# Breakout-setup detector
# ----------------------------------------------------------------------------
@dataclass
class Setup:
    idx: int
    side: str            # 'long' | 'short'
    level: float
    score: float
    squeeze: bool
    one_sided: float
    detail: dict


def detect_setups(bars, *, lookback=480, base_bars=120, pivot_order=5,
                  atr_period=14, cluster_atr_mult=0.5, min_touches=3,
                  brk=0.0015, min_score=0.0, require_squeeze=True,
                  one_sided_min=0.75, near_atr=1.0, cooldown=30, vp_bins=120):
    """Detect decisive breakouts of a HIGH-QUALITY level out of a squeezed base.

    Quality gates that fix the naive detector:
      * level confirmed by volume profile + bounce + tightness (score)
      * level is the BASE edge: one-sidedness measured on the recent `base_bars`
        only (not stale history) → rejects mid-range pivots in chop (POPCAT bug)
      * squeeze (BB-in-KC) active in the base → real coil, not random range
      * decisive close beyond the level
    """
    o = np.array([b["open"] for b in bars], dtype="float64")
    h = np.array([b["high"] for b in bars], dtype="float64")
    l = np.array([b["low"] for b in bars], dtype="float64")
    c = np.array([b["close"] for b in bars], dtype="float64")
    v = np.array([b.get("volume", 0.0) for b in bars], dtype="float64")
    a = atr(h, l, c, atr_period)
    sq = ttm_squeeze(h, l, c)

    out, last = [], -10 ** 9
    for i in range(lookback, len(bars)):
        if i - last < cooldown or np.isnan(a[i]) or a[i] <= 0:
            continue
        piv = find_pivots(h[i - lookback:i], l[i - lookback:i], pivot_order)
        piv = [(idx + (i - lookback), px, k) for idx, px, k in piv]
        if len(piv) < min_touches:
            continue
        centers, mass = volume_profile(c[i - lookback:i], v[i - lookback:i],
                                       lo=l[i - lookback:i].min(),
                                       hi=h[i - lookback:i].max(), bins=vp_bins)
        clusters = cluster_pivots(piv, a[i] * cluster_atr_mult)
        base_lo = i - base_bars
        base_closes = c[base_lo:i]
        fired = False
        for side in ("long", "short"):
            cands = []
            for cl in clusters:
                if cl["n"] < min_touches:
                    continue
                L = cl["price"]
                # near the level now, broke decisively this bar
                broke = ((side == "long" and c[i - 1] <= L and c[i] > L * (1 + brk)) or
                         (side == "short" and c[i - 1] >= L and c[i] < L * (1 - brk)))
                if not broke:
                    continue
                if abs(c[i - 1] - L) > near_atr * a[i]:
                    continue
                os = (np.mean(base_closes < L) if side == "long"
                      else np.mean(base_closes > L))
                if os < one_sided_min:
                    continue
                if require_squeeze and not sq[base_lo:i].any():
                    continue
                s, det = score_level(cl, h, l, c, i, centers, mass, a[i])
                if s < min_score:
                    continue
                det["squeeze_frac"] = round(float(sq[base_lo:i].mean()), 2)
                cands.append(Setup(i, side, L, s, bool(sq[base_lo:i].any()),
                                   float(os), det))
            if cands:
                best = max(cands, key=lambda x: x.score)
                out.append(best)
                last, fired = i, True
                break
        if fired:
            continue
    return out
