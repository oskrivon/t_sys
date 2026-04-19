"""
ML + Vision >= 8 Combined Pipeline Test.

Walk-forward: train ML on 8 months, test on 2 months.
On test trades that pass ML threshold, score with Claude Vision.
Measure: WR and annual return of ML+Vision pipeline.

Cost: ~$5-8 for Vision API calls.

Usage:
    python scripts/research/backtest_ml_vision_combined.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier

from dotenv import load_dotenv
load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY", "")

from src.strategy.levels import get_rolling_levels, find_swing_points
from src.strategy.signals import detect_breakouts, detect_retests, detect_zakol, build_signal
from src.strategy.features import compute_features
from src.strategy.models import Breakout, Level

DATA = ROOT / "data" / "processed" / "candles"
REPORTS = ROOT / "data" / "reports"

# Full 50 symbols for ML, Vision applied on ML-filtered
SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT",
    "AAVE/USDT", "BNB/USDT", "TRX/USDT", "LINK/USDT", "ADA/USDT",
    "AVAX/USDT", "NEAR/USDT", "LTC/USDT", "FET/USDT", "UNI/USDT",
    "FIL/USDT", "DOT/USDT", "DYDX/USDT", "AR/USDT", "HBAR/USDT",
    "XLM/USDT", "SHIB/USDT", "COMP/USDT", "BCH/USDT", "ICP/USDT",
    "CRV/USDT", "AXS/USDT", "ALGO/USDT", "CAKE/USDT", "APE/USDT",
    "OP/USDT", "ARB/USDT", "SUI/USDT", "PEPE/USDT", "INJ/USDT",
    "TIA/USDT", "WIF/USDT", "ONDO/USDT", "RENDER/USDT",
    "ATOM/USDT", "ETC/USDT", "APT/USDT", "MANTA/USDT", "SEI/USDT",
    "JUP/USDT", "WLD/USDT", "STRK/USDT", "PENDLE/USDT", "ENA/USDT",
    "TAO/USDT", "GALA/USDT",
]

FEE_BPS = 10


# ---- Chart & Vision (from vision_oos_test.py) ----

def generate_chart_b64(df, entry_idx, levels, signal_type, is_long):
    import base64, io, matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    start = max(0, entry_idx - 60)
    end = min(len(df), entry_idx + 20)
    chunk = df.iloc[start:end].copy()
    if len(chunk) < 10:
        return ""
    fig, ax = plt.subplots(figsize=(12, 6), facecolor="#1a1a2e")
    ax.set_facecolor("#1a1a2e")
    for i, (_, row) in enumerate(chunk.iterrows()):
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        color = "#26a69a" if c >= o else "#ef5350"
        ax.bar(i, abs(c-o), bottom=min(o,c), width=0.6, color=color, edgecolor=color)
        ax.plot([i,i], [l, min(o,c)], color=color, linewidth=0.8)
        ax.plot([i,i], [min(o,c)+abs(c-o), h], color=color, linewidth=0.8)
    entry_price = df["close"].iloc[entry_idx]
    pr = chunk["high"].max() - chunk["low"].min()
    for lvl in levels:
        if chunk["low"].min() - pr*0.1 < lvl < chunk["high"].max() + pr*0.1:
            ax.axhline(y=lvl, color="#4caf50" if lvl < entry_price else "#f44336",
                       linewidth=1, alpha=0.7, linestyle="--")
    el = entry_idx - start
    if 0 <= el < len(chunk):
        ax.axvline(x=el, color="#2196f3", linewidth=1.5, alpha=0.6, linestyle=":")
        ac = "#26a69a" if is_long else "#ef5350"
        ad = 1 if is_long else -1
        ax.annotate("LONG" if is_long else "SHORT", xy=(el, entry_price),
                    xytext=(el+3, entry_price + ad*pr*0.08), color=ac, fontsize=10,
                    fontweight="bold", arrowprops=dict(arrowstyle="->", color=ac, lw=2))
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    ax.spines["bottom"].set_color("#444"); ax.spines["left"].set_color("#444")
    ax.tick_params(colors="#888")
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def call_vision(image_b64, symbol, signal_type, is_long):
    import openai
    client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=API_KEY)
    direction = "LONG (buy)" if is_long else "SHORT (sell)"
    prompt = f"""You are an expert crypto trader evaluating chart setups.
This is a 4h candlestick chart for {symbol}.
Green lines = support, Red lines = resistance.
Blue line = {signal_type} signal, direction: {direction}.
Evaluate setup quality 1-10. Respond ONLY with JSON: {{"score": N, "reason": "brief"}}"""
    try:
        resp = client.chat.completions.create(
            model="anthropic/claude-sonnet-4", max_tokens=200,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                {"type": "text", "text": prompt}]}])
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except json.JSONDecodeError:
        import re
        m = re.search(r'"score"\s*:\s*(\d+)', text)
        return {"score": int(m.group(1)), "reason": text[:200]} if m else {"score": -1, "reason": text[:200]}
    except Exception as e:
        return {"score": -1, "reason": str(e)}


# ---- Level quality (slim) ----

def compute_lq_slim(df, level, idx, lookback=200, swing_order=5):
    start = max(0, idx - lookback)
    end = idx - swing_order
    if end - start < 20: return {}
    points = find_swing_points(df, start, end, order=swing_order)
    tol = level.zone_high - level.zone_low
    if tol <= 0: tol = level.price * 0.01
    touches = [(i,p,t) for i,p,t in points if abs(p - level.price) <= tol*1.2]
    if not touches: return {}
    f = {}
    vols = []
    for i,p,t in touches:
        if 30 <= i < len(df):
            av = df["volume"].iloc[max(0,i-30):i].mean()
            if av > 0: vols.append(df["volume"].iloc[i]/av)
    f["lq_max_touch_vol"] = float(np.max(vols)) if vols else 1.0
    f["lq_touch_vol_trend"] = float(vols[-1]/vols[0]) if len(vols)>1 and vols[0]>0 else 1.0
    wicks = []
    for i,p,t in touches:
        if i >= len(df): continue
        cr = df["high"].iloc[i] - df["low"].iloc[i]
        if cr <= 0: continue
        w = (min(df["close"].iloc[i],df["open"].iloc[i])-df["low"].iloc[i]) if t=="low" else (df["high"].iloc[i]-max(df["close"].iloc[i],df["open"].iloc[i]))
        wicks.append(w/cr)
    f["lq_max_wick_rejection"] = float(np.max(wicks)) if wicks else 0.0
    tp = [t[1] for t in touches]
    f["lq_zone_tightness"] = float(np.std(tp)/level.price*100) if len(tp)>=2 else 0.0
    return f


# ---- Data loading & trade collection ----

def load_data(tf):
    ds = {}
    for s in SYMBOLS:
        p = DATA / f"{s.replace('/','')}{tf}.parquet"
        if p.exists(): ds[s] = pd.read_parquet(p)
    return ds


def get_d1_levels(d1_df, ts):
    mask = d1_df["ts"] <= ts
    if mask.sum() < 50: return []
    return get_rolling_levels(d1_df[mask], mask.sum()-1, lookback=120, min_touches=2,
                              tolerance_pct=1.5, min_level_age=5, swing_order=3)


def collect_trades(entry_data, d1_data, params):
    records = []
    for symbol in SYMBOLS:
        if symbol not in entry_data or symbol not in d1_data: continue
        df = entry_data[symbol]; d1 = d1_data[symbol]
        sma = df["close"].rolling(params["trend_sma"]).mean()
        trend = pd.Series(0, index=df.index)
        trend[df["close"]>sma]=1; trend[df["close"]<sma]=-1
        at = None; brks = []; lvls = []; lc = 0; ld1 = None
        wu = max(params["level_lookback"], params["trend_sma"])+10
        for i in range(wu, len(df)):
            ts = df["ts"].iloc[i]
            if at:
                h,l = df["high"].iloc[i], df["low"].iloc[i]
                out, ep = None, None
                if at["is_long"]:
                    if l<=at["sl"]: out,ep="loss",at["sl"]
                    elif h>=at["tp"]: out,ep="win",at["tp"]
                    elif i-at["ei"]>params["max_hold"]: out,ep="timeout",df["close"].iloc[i]
                else:
                    if h>=at["sl"]: out,ep="loss",at["sl"]
                    elif l<=at["tp"]: out,ep="win",at["tp"]
                    elif i-at["ei"]>params["max_hold"]: out,ep="timeout",df["close"].iloc[i]
                if out:
                    e = at["entry_price"]
                    pnl = ((ep-e)/e if at["is_long"] else (e-ep)/e) - 2*FEE_BPS/10000
                    records.append({**at["features"], "outcome":out, "pnl_pct":pnl,
                        "symbol":symbol, "signal_type":at["st"], "is_long":at["is_long"],
                        "entry_idx":at["ei"], "entry_ts":at["ets"], "level_price":at["lp"]})
                    at = None
                else: continue
            if ld1 is None or (ts-ld1).total_seconds()>86400:
                lvls = get_d1_levels(d1, ts); ld1 = ts
            if i-lc >= params["check_interval"]:
                lc = i
                nb = detect_breakouts(df, i, lvls); brks.extend(nb)
                brks = [b for b in brks if i-b.idx<=params["retest_window"]]
            ci = df["close"].iloc[i]
            rt = detect_retests(df, i, brks, params["retest_window"], trend)
            zk = detect_zakol(df, i, lvls, trend)
            best = rt if rt and (not zk or rt[2]>=zk[2]) else zk
            if best:
                st, lv, _ = best
                sig = build_signal(symbol, st, lv, ci, params["rr_ratio"])
                if not sig: continue
                feat = compute_features(df, i, lv, st, params["trend_sma"])
                lq = compute_lq_slim(df, lv, i, lookback=params["level_lookback"])
                feat.update(lq)
                at = {"ei":i, "entry_price":ci, "sl":sig.sl, "tp":sig.tp,
                      "is_long":sig.is_long, "st":st.value, "features":feat,
                      "ets":ts, "lp":lv.price, "symbol":symbol}
    return pd.DataFrame(records)


def main():
    print("=== ML + Vision >= 8 Combined Pipeline ===\n")
    if not API_KEY:
        print("ERROR: OPENROUTER_API_KEY not set"); return

    d1 = load_data("_1d"); d4h = load_data("_4h")
    print(f"D1: {len(d1)}, 4H: {len(d4h)} symbols")

    params = dict(level_lookback=200, level_tolerance_pct=1.0, retest_window=15,
                  min_level_age=20, trend_sma=50, rr_ratio=3.0, check_interval=6, max_hold=150)

    print("\nCollecting all trades...")
    all_trades = collect_trades(d4h, d1, params)
    wl = all_trades[all_trades["outcome"].isin(["win","loss"])].copy()
    wl["target"] = (wl["outcome"]=="win").astype(int)
    wl["entry_ts"] = pd.to_datetime(wl["entry_ts"])
    wl = wl.sort_values("entry_ts")
    print(f"Total: {len(wl)} W/L trades ({wl['target'].sum()}W/{(~wl['target'].astype(bool)).sum()}L)")

    # Walk-forward ML
    exclude = {"outcome","target","pnl_pct","symbol","signal_type","is_long",
               "hold_candles","entry_ts","entry_idx","level_price","d1_aligned"}
    feature_cols = [c for c in wl.columns if c not in exclude]

    train_delta = pd.Timedelta(days=240)  # 8 months
    test_delta = pd.Timedelta(days=60)    # 2 months
    min_ts = wl["entry_ts"].min()
    max_ts = wl["entry_ts"].max()

    ml_filtered_trades = []
    current = min_ts
    window = 0

    while current + train_delta + test_delta <= max_ts + pd.Timedelta(days=1):
        train_end = current + train_delta
        test_end = train_end + test_delta
        train = wl[(wl["entry_ts"]>=current) & (wl["entry_ts"]<train_end)]
        test = wl[(wl["entry_ts"]>=train_end) & (wl["entry_ts"]<test_end)]

        if len(train)<30 or len(test)<5:
            current += test_delta; window += 1; continue

        # Feature selection on train
        X_tr = train[feature_cols].fillna(0).values
        y_tr = train["target"].values
        sel = GradientBoostingClassifier(n_estimators=50, max_depth=2, min_samples_leaf=15,
                                          subsample=0.8, random_state=42)
        sel.fit(X_tr, y_tr)
        fi = sorted(zip(feature_cols, sel.feature_importances_), key=lambda x:x[1], reverse=True)
        top_cols = [n for n,v in fi[:10]]

        # Train ML
        X_tr = train[top_cols].fillna(0).values
        clf = GradientBoostingClassifier(n_estimators=80, max_depth=3, learning_rate=0.1,
                                          min_samples_leaf=15, subsample=0.8, random_state=42)
        clf.fit(X_tr, y_tr)

        # Predict on test
        X_te = test[top_cols].fillna(0).values
        probs = clf.predict_proba(X_te)[:,1]

        # Filter by ML threshold 0.50
        for idx, prob in zip(test.index, probs):
            if prob >= 0.50:
                row = test.loc[idx]
                ml_filtered_trades.append({
                    "idx": idx, "ml_prob": prob, "window": window,
                    "symbol": row["symbol"], "signal_type": row["signal_type"],
                    "is_long": row["is_long"], "entry_idx": int(row["entry_idx"]),
                    "level_price": row["level_price"],
                    "outcome": row["outcome"], "target": row["target"],
                    "pnl_pct": row["pnl_pct"],
                })

        print(f"  Window {window}: train {len(train)}, test {len(test)}, "
              f"ML passed: {sum(1 for _,p in zip(test.index, probs) if p>=0.50)}")
        current += test_delta; window += 1

    ml_df = pd.DataFrame(ml_filtered_trades)
    print(f"\nML-filtered: {len(ml_df)} trades, WR {ml_df['target'].mean()*100:.1f}%")

    # Score ML-filtered trades with Vision
    print(f"\nScoring {len(ml_df)} trades with Vision (~${len(ml_df)*0.01:.1f})...\n")

    vision_results = []
    for i, (_, row) in enumerate(ml_df.iterrows()):
        symbol = row["symbol"]
        key = symbol.replace("/", "")
        path = DATA / f"{key}_4h.parquet"
        if not path.exists(): continue
        df = pd.read_parquet(path)
        entry_idx = row["entry_idx"]
        if entry_idx >= len(df): continue

        chart = generate_chart_b64(df, entry_idx, [row["level_price"]],
                                    row["signal_type"], row["is_long"])
        if not chart: continue

        result = call_vision(chart, symbol, row["signal_type"], row["is_long"])
        score = result.get("score", -1)

        if score >= 0:
            vision_results.append({**row.to_dict(), "vision_score": score})
            outcome_str = "WIN " if row["target"] else "LOSS"
            print(f"  [{i+1}/{len(ml_df)}] {symbol:14s} score={score} {outcome_str} "
                  f"(ml={row['ml_prob']:.2f})")
        else:
            print(f"  [{i+1}/{len(ml_df)}] {symbol:14s} FAILED: {result.get('reason','')[:50]}")

        time.sleep(0.5)

    vdf = pd.DataFrame(vision_results)
    REPORTS.mkdir(parents=True, exist_ok=True)
    vdf.to_csv(REPORTS / "ml_vision_combined_results.csv", index=False)

    # Analysis
    print(f"\n{'='*65}")
    print(f"  RESULTS")
    print(f"{'='*65}")

    if vdf.empty:
        print("  No results!"); return

    total_days = (pd.to_datetime(ml_df.iloc[-1]["entry_ts"] if "entry_ts" in ml_df.columns
                  else pd.Timestamp.now()) -
                  pd.to_datetime(ml_df.iloc[0]["entry_ts"] if "entry_ts" in ml_df.columns
                  else pd.Timestamp.now())).days
    period_months = max(1, total_days / 30)
    risk_pct = 4.0

    # Compare: ML only vs ML + Vision
    pipelines = [
        ("ML only (thr>=0.50)", vdf),
    ]
    for v_thr in [6, 7, 8]:
        sub = vdf[vdf["vision_score"] >= v_thr]
        if len(sub) >= 5:
            pipelines.append((f"ML + Vision>={v_thr}", sub))

    print(f"\n  {'Pipeline':30s} {'N':>5} {'T/mo':>5} {'WR':>6} {'PF':>6} {'Exp':>9} {'Ann':>8}")
    for name, sub in pipelines:
        n = len(sub)
        tpm = n / period_months
        fy = sub["target"].values
        fp = sub["pnl_pct"].values
        wr = fy.mean()
        aw = fp[fy==1].mean() if (fy==1).sum()>0 else 0
        al = abs(fp[fy==0].mean()) if (fy==0).sum()>0 else 0
        exp = wr*aw - (1-wr)*al
        gw = fp[fy==1].sum() if (fy==1).sum()>0 else 0
        gl = abs(fp[fy==0].sum()) if (fy==0).sum()>0 else 1
        pf = gw/gl if gl>0 else 0
        monthly = tpm * exp * risk_pct / 100
        annual = (1+monthly)**12 - 1
        print(f"  {name:30s} {n:>5} {tpm:>4.0f} {wr*100:>5.1f}% {pf:>5.2f} "
              f"{exp*100:>+8.3f}% {annual*100:>+7.1f}%")

    # Vision score distribution
    print(f"\n  Vision score distribution (ML-filtered trades):")
    for s in sorted(vdf["vision_score"].unique()):
        sub = vdf[vdf["vision_score"]==s]
        print(f"    Score {s}: {len(sub)} trades, WR {sub['target'].mean()*100:.0f}%")


if __name__ == "__main__":
    main()
