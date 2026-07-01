"""
Google Trends "mercury retrograde" vs BTC volatility/returns.

Tests:
1. Correlation between search interest spikes and BTC volatility
2. Does elevated "mercury retrograde" search predict next-week returns?
3. Granger causality: do searches lead price moves or vice versa?
"""

import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pytrends.request import TrendReq
import ccxt.async_support as ccxt
from scipy import stats
import time


def fetch_google_trends(keywords: list[str], timeframe: str = "2017-08-01 2026-05-23") -> pd.DataFrame:
    """Fetch weekly Google Trends data for keywords."""
    pytrends = TrendReq(hl='en-US', tz=0)

    # Google Trends limits to 5 years per request, so split if needed
    start_str, end_str = timeframe.split(" ")
    start = pd.Timestamp(start_str)
    end = pd.Timestamp(end_str)

    chunks = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + pd.DateOffset(years=4, months=11), end)
        tf = f"{chunk_start.strftime('%Y-%m-%d')} {chunk_end.strftime('%Y-%m-%d')}"
        print(f"  Fetching trends: {tf}")

        pytrends.build_payload(keywords, timeframe=tf, geo='')
        df = pytrends.interest_over_time()
        if not df.empty and 'isPartial' in df.columns:
            df = df.drop(columns=['isPartial'])
        chunks.append(df)

        chunk_start = chunk_end + pd.DateOffset(days=1)
        time.sleep(2)  # rate limit

    if not chunks:
        return pd.DataFrame()

    # If multiple chunks, normalize overlap
    if len(chunks) == 1:
        return chunks[0]

    # Stitch chunks by scaling to the first chunk's level
    combined = chunks[0]
    for i in range(1, len(chunks)):
        # Find overlap period (last 4 weeks of prev, first 4 weeks of next)
        prev_end = combined.index[-1]
        next_start = chunks[i].index[0]

        if prev_end >= next_start:
            overlap_prev = combined.loc[next_start:prev_end]
            overlap_next = chunks[i].loc[next_start:prev_end]
            if len(overlap_prev) > 0 and len(overlap_next) > 0:
                for kw in keywords:
                    if kw in overlap_prev.columns and kw in overlap_next.columns:
                        scale = overlap_prev[kw].mean() / max(overlap_next[kw].mean(), 1)
                        chunks[i][kw] = (chunks[i][kw] * scale).astype(int)

            # Append only non-overlapping part
            new_data = chunks[i].loc[prev_end + pd.Timedelta(days=1):]
            combined = pd.concat([combined, new_data])
        else:
            combined = pd.concat([combined, chunks[i]])

    return combined


async def fetch_btc_weekly() -> pd.DataFrame:
    """Fetch BTC/USDT weekly candles."""
    exchange = ccxt.binance({"enableRateLimit": True})
    try:
        since = exchange.parse8601("2017-08-01T00:00:00Z")
        all_candles = []
        while True:
            candles = await exchange.fetch_ohlcv(
                "BTC/USDT", "1w", since=since, limit=1000
            )
            if not candles:
                break
            all_candles.extend(candles)
            since = candles[-1][0] + 7 * 86400000
            if len(candles) < 1000:
                break

        df = pd.DataFrame(
            all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["date"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.set_index("date")
        df["returns"] = df["close"].pct_change()
        df["volatility"] = df["returns"].rolling(4).std()  # 4-week rolling vol
        df["abs_return"] = df["returns"].abs()
        # Intra-week volatility proxy: (high-low)/open
        df["range_pct"] = (df["high"] - df["low"]) / df["open"]
        return df
    finally:
        await exchange.close()


def align_data(trends_df: pd.DataFrame, btc_df: pd.DataFrame, keyword: str) -> pd.DataFrame:
    """Align weekly Google Trends and BTC data."""
    # Normalize index dtypes for merge_asof compatibility
    left = trends_df[[keyword]].sort_index()
    right = btc_df[["returns", "volatility", "abs_return", "range_pct", "close", "volume"]].sort_index()
    left.index = pd.to_datetime(left.index).tz_localize(None) if left.index.tz else pd.to_datetime(left.index)
    right.index = pd.to_datetime(right.index).tz_localize(None) if right.index.tz else pd.to_datetime(right.index)
    # Force same resolution
    left.index = left.index.as_unit("ns")
    right.index = right.index.as_unit("ns")

    merged = pd.merge_asof(
        left, right,
        left_index=True, right_index=True,
        direction="nearest",
        tolerance=pd.Timedelta(days=3),
    )
    merged = merged.dropna()
    return merged


def analyze_correlation(df: pd.DataFrame, keyword: str) -> dict:
    """Run correlation and lead/lag analysis."""
    results = {}

    # 1. Contemporaneous correlation
    corr_ret, p_ret = stats.pearsonr(df[keyword], df["returns"])
    corr_vol, p_vol = stats.pearsonr(df[keyword], df["abs_return"])
    corr_range, p_range = stats.pearsonr(df[keyword], df["range_pct"])

    results["contemporaneous"] = {
        "corr_returns": corr_ret, "p_returns": p_ret,
        "corr_abs_return": corr_vol, "p_abs_return": p_vol,
        "corr_range": corr_range, "p_range": p_range,
    }

    # 2. Lead/lag: does search THIS week predict NEXT week?
    df = df.copy()
    df["next_return"] = df["returns"].shift(-1)
    df["next_abs_return"] = df["abs_return"].shift(-1)
    df["next_range"] = df["range_pct"].shift(-1)
    df["prev_return"] = df["returns"].shift(1)

    clean = df.dropna()

    corr_next_ret, p_next_ret = stats.pearsonr(clean[keyword], clean["next_return"])
    corr_next_vol, p_next_vol = stats.pearsonr(clean[keyword], clean["next_abs_return"])
    corr_prev_ret, p_prev_ret = stats.pearsonr(clean[keyword], clean["prev_return"])

    results["predictive"] = {
        "search_predicts_next_return": corr_next_ret, "p_value": p_next_ret,
        "search_predicts_next_vol": corr_next_vol, "p_value_vol": p_next_vol,
        "prev_return_predicts_search": corr_prev_ret, "p_prev": p_prev_ret,
    }

    # 3. Quintile analysis: split by search interest level
    df["search_quintile"] = pd.qcut(df[keyword], 5, labels=False, duplicates="drop")
    quintile_stats = []
    for q in sorted(df["search_quintile"].dropna().unique()):
        subset = df[df["search_quintile"] == q]
        quintile_stats.append({
            "quintile": int(q),
            "n": len(subset),
            "search_range": f"{subset[keyword].min()}-{subset[keyword].max()}",
            "mean_return": subset["returns"].mean(),
            "mean_abs_return": subset["abs_return"].mean(),
            "mean_range": subset["range_pct"].mean(),
            "positive_pct": (subset["returns"] > 0).mean(),
        })
    results["quintiles"] = quintile_stats

    # 4. Spike analysis: weeks where search > 2 std above mean
    mean_search = df[keyword].mean()
    std_search = df[keyword].std()
    threshold = mean_search + 1.5 * std_search
    spikes = df[df[keyword] > threshold]
    normal = df[df[keyword] <= threshold]

    results["spike_analysis"] = {
        "threshold": threshold,
        "n_spike_weeks": len(spikes),
        "n_normal_weeks": len(normal),
        "spike_mean_return": spikes["returns"].mean() if len(spikes) > 0 else 0,
        "normal_mean_return": normal["returns"].mean() if len(normal) > 0 else 0,
        "spike_mean_vol": spikes["abs_return"].mean() if len(spikes) > 0 else 0,
        "normal_mean_vol": normal["abs_return"].mean() if len(normal) > 0 else 0,
        "spike_range": spikes["range_pct"].mean() if len(spikes) > 0 else 0,
        "normal_range": normal["range_pct"].mean() if len(normal) > 0 else 0,
    }

    if len(spikes) > 2 and len(normal) > 2:
        t, p = stats.ttest_ind(spikes["returns"].dropna(), normal["returns"].dropna())
        results["spike_analysis"]["t_stat"] = t
        results["spike_analysis"]["p_value"] = p

    return results


def print_report(results: dict, keyword: str, df: pd.DataFrame):
    """Print analysis report."""
    print("=" * 70)
    print(f"  GOOGLE TRENDS '{keyword}' vs BTC")
    print(f"  Period: {df.index[0].date()} to {df.index[-1].date()} ({len(df)} weeks)")
    print("=" * 70)

    # Contemporaneous
    c = results["contemporaneous"]
    print("\n-- Contemporaneous Correlation (same week) --")
    print(f"  Search vs Return:      r={c['corr_returns']:+.4f}  p={c['p_returns']:.4f}  {'*' if c['p_returns'] < 0.05 else ''}")
    print(f"  Search vs |Return|:    r={c['corr_abs_return']:+.4f}  p={c['p_abs_return']:.4f}  {'*' if c['p_abs_return'] < 0.05 else ''}")
    print(f"  Search vs Range:       r={c['corr_range']:+.4f}  p={c['p_range']:.4f}  {'*' if c['p_range'] < 0.05 else ''}")

    # Predictive
    p = results["predictive"]
    print("\n-- Lead/Lag Analysis --")
    print(f"  Search -> Next week return:   r={p['search_predicts_next_return']:+.4f}  p={p['p_value']:.4f}  {'*' if p['p_value'] < 0.05 else ''}")
    print(f"  Search -> Next week |ret|:    r={p['search_predicts_next_vol']:+.4f}  p={p['p_value_vol']:.4f}  {'*' if p['p_value_vol'] < 0.05 else ''}")
    print(f"  Prev return -> Search:        r={p['prev_return_predicts_search']:+.4f}  p={p['p_prev']:.4f}  {'*' if p['p_prev'] < 0.05 else ''}")

    # Quintiles
    print("\n-- Quintile Analysis (by search interest level) --")
    print(f"  {'Q':>3s}  {'N':>4s}  {'Search':>10s}  {'MeanRet':>10s}  {'MeanVol':>10s}  {'Range':>8s}  {'Pos%':>5s}")
    for q in results["quintiles"]:
        print(f"  {q['quintile']:3d}  {q['n']:4d}  {q['search_range']:>10s}  "
              f"{q['mean_return']*100:+8.3f}%  {q['mean_abs_return']*100:8.3f}%  "
              f"{q['mean_range']*100:6.1f}%  {q['positive_pct']*100:4.0f}%")

    # Spike analysis
    s = results["spike_analysis"]
    print(f"\n-- Spike Analysis (search > {s['threshold']:.0f}, i.e. mean + 1.5 std) --")
    print(f"  Spike weeks:  {s['n_spike_weeks']} / {s['n_spike_weeks'] + s['n_normal_weeks']}")
    print(f"  Spike mean return:   {s['spike_mean_return']*100:+.3f}%")
    print(f"  Normal mean return:  {s['normal_mean_return']*100:+.3f}%")
    print(f"  Spike mean |return|: {s['spike_mean_vol']*100:.3f}%")
    print(f"  Normal mean |return|:{s['normal_mean_vol']*100:.3f}%")
    print(f"  Spike mean range:    {s['spike_range']*100:.1f}%")
    print(f"  Normal mean range:   {s['normal_range']*100:.1f}%")
    if "t_stat" in s:
        sig = "YES" if s["p_value"] < 0.05 else "NO"
        print(f"  T-test spike vs normal returns: t={s['t_stat']:.3f}, p={s['p_value']:.4f} -> Significant: {sig}")

    print("\n-- INTERPRETATION --")
    # Auto-interpret
    has_contemp = abs(c["corr_abs_return"]) > 0.1 and c["p_abs_return"] < 0.05
    has_predict = abs(p["search_predicts_next_return"]) > 0.1 and p["p_value"] < 0.05
    has_spike = "p_value" in s and s["p_value"] < 0.05

    if has_contemp:
        direction = "higher" if c["corr_abs_return"] > 0 else "lower"
        print(f"  [!] Search interest correlates with {direction} volatility same-week")
    if has_predict:
        direction = "positive" if p["search_predicts_next_return"] > 0 else "negative"
        print(f"  [!] Search interest PREDICTS {direction} returns next week")
    if has_spike:
        print(f"  [!] Spike weeks have significantly different returns")
    if not (has_contemp or has_predict or has_spike):
        print("  No significant relationship found between search interest and BTC")

    print("=" * 70)


async def main():
    # Fetch BTC data
    print("Fetching BTC/USDT weekly data...")
    btc_df = await fetch_btc_weekly()
    print(f"  {len(btc_df)} weekly candles")

    # Fetch Google Trends
    print("\nFetching Google Trends data...")
    keywords = ["mercury retrograde"]
    try:
        trends_df = fetch_google_trends(keywords)
    except Exception as e:
        print(f"  Google Trends API error: {e}")
        print("  Trying with shorter timeframe...")
        trends_df = fetch_google_trends(keywords, "2020-01-01 2026-05-23")

    if trends_df.empty:
        print("ERROR: Could not fetch Google Trends data")
        return

    print(f"  {len(trends_df)} weekly data points")

    # Also try "horoscope" and "astrology" for comparison
    extra_keywords = ["horoscope", "astrology"]
    print(f"\nFetching comparison keywords: {extra_keywords}")
    try:
        extra_df = fetch_google_trends(extra_keywords)
    except Exception as e:
        print(f"  Skipping extra keywords: {e}")
        extra_df = pd.DataFrame()

    # Analyze primary keyword
    keyword = "mercury retrograde"
    merged = align_data(trends_df, btc_df, keyword)
    print(f"\nAligned dataset: {len(merged)} weeks")

    results = analyze_correlation(merged, keyword)
    print_report(results, keyword, merged)

    # Analyze extra keywords if available
    if not extra_df.empty:
        for kw in extra_keywords:
            if kw in extra_df.columns:
                merged_extra = align_data(extra_df, btc_df, kw)
                if len(merged_extra) > 20:
                    results_extra = analyze_correlation(merged_extra, kw)
                    print_report(results_extra, kw, merged_extra)

    # Save merged data
    output_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'reports')
    os.makedirs(output_dir, exist_ok=True)
    merged.to_csv(os.path.join(output_dir, "gtrends_mercury_btc.csv"))
    print(f"\nData saved to {output_dir}/gtrends_mercury_btc.csv")


if __name__ == "__main__":
    asyncio.run(main())
