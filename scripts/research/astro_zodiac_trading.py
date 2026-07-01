"""
Zodiac Trading Backtest: do "good/bad horoscope days" for specific
signs correlate with BTC returns?

Hypothesis: superstitious retail traders (Cancer, Pisces) check daily
horoscopes and trade accordingly. If enough of them act on it,
"bad astro days" for the dominant sign should show different returns.

Method:
1. Calculate daily planetary transits using ephem
2. Score each day as good/bad for each zodiac sign using traditional rules
3. Correlate astro-score with BTC returns
4. Test all 12 signs, highlight Cancer (most anxious) and Pisces (most mystical)
"""

import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import ephem
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from scipy import stats
import ccxt.async_support as ccxt

# Zodiac signs with their ecliptic longitude ranges
SIGNS = [
    ("Aries", 0, 30),
    ("Taurus", 30, 60),
    ("Gemini", 60, 90),
    ("Cancer", 90, 120),
    ("Leo", 120, 150),
    ("Virgo", 150, 180),
    ("Libra", 180, 210),
    ("Scorpio", 210, 240),
    ("Sagittarius", 240, 270),
    ("Capricorn", 270, 300),
    ("Aquarius", 300, 330),
    ("Pisces", 330, 360),
]

# Aspect definitions: (name, angle, orb, score)
# Positive aspects: conjunction (sometimes), trine, sextile
# Negative aspects: square, opposition
ASPECTS = [
    ("conjunction", 0, 8, 0),      # neutral, depends on planet
    ("sextile", 60, 5, +1),        # mildly positive
    ("square", 90, 7, -2),         # negative / tension
    ("trine", 120, 7, +2),         # positive / flow
    ("opposition", 180, 8, -1.5),  # challenging
]

# Planet weights for horoscope scoring
# Benefics (Venus, Jupiter) in good aspect = extra positive
# Malefics (Mars, Saturn) in bad aspect = extra negative
PLANET_WEIGHTS = {
    "Moon": 2.0,      # fastest, most emotional impact in horoscopes
    "Mercury": 1.0,
    "Venus": 1.5,     # benefic
    "Mars": 1.5,      # malefic
    "Jupiter": 1.5,   # great benefic
    "Saturn": 2.0,    # great malefic - fear factor
}

# For malefics, flip the aspect score
MALEFICS = {"Mars", "Saturn"}


def get_ecliptic_longitude(body, date) -> float:
    """Get ecliptic longitude of a body in degrees (0-360)."""
    body.compute(date)
    # ephem gives ra/dec, need ecliptic
    ecl = ephem.Ecliptic(body)
    return float(ecl.lon) * 180.0 / np.pi  # radians to degrees


def get_sign_midpoint(sign_idx: int) -> float:
    """Get the midpoint longitude of a zodiac sign (0-11)."""
    return sign_idx * 30 + 15  # midpoint of the sign


def angle_diff(a: float, b: float) -> float:
    """Smallest angle between two ecliptic longitudes."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


def score_day_for_sign(date_str: str, sign_idx: int) -> dict:
    """
    Score a day for a zodiac sign based on transiting planets.

    Simulates what a horoscope writer would say:
    - Moon in trine/sextile to your sign = good emotional day
    - Saturn square your sign = heavy, restricted
    - Jupiter conjunct/trine = lucky, expansive
    - Mars square/opposition = conflict, aggression
    """
    d = ephem.Date(date_str)
    sign_midpoint = get_sign_midpoint(sign_idx)

    planets = {
        "Moon": ephem.Moon(),
        "Mercury": ephem.Mercury(),
        "Venus": ephem.Venus(),
        "Mars": ephem.Mars(),
        "Jupiter": ephem.Jupiter(),
        "Saturn": ephem.Saturn(),
    }

    total_score = 0
    aspects_found = []

    for planet_name, planet_body in planets.items():
        planet_lon = get_ecliptic_longitude(planet_body, d)
        weight = PLANET_WEIGHTS[planet_name]

        for aspect_name, aspect_angle, orb, base_score in ASPECTS:
            diff = angle_diff(planet_lon, sign_midpoint)
            if abs(diff - aspect_angle) <= orb:
                # For malefics, negative aspects are extra bad,
                # positive aspects are weakened
                if planet_name in MALEFICS:
                    if base_score < 0:
                        score = base_score * weight * 1.5  # extra bad
                    else:
                        score = base_score * weight * 0.5  # weakened good
                else:
                    if base_score > 0:
                        score = base_score * weight * 1.3  # benefics amplify good
                    else:
                        score = base_score * weight

                total_score += score
                aspects_found.append({
                    "planet": planet_name,
                    "aspect": aspect_name,
                    "score": score,
                })
                break  # one aspect per planet

    return {
        "score": total_score,
        "n_aspects": len(aspects_found),
        "aspects": aspects_found,
    }


def calculate_all_days(start_date: str, end_date: str) -> pd.DataFrame:
    """Calculate astro scores for all signs for every day in range."""
    dates = pd.date_range(start_date, end_date, freq='D')
    records = []

    for i, date in enumerate(dates):
        if i % 365 == 0:
            print(f"  Processing {date.year}...")
        date_str = date.strftime("%Y/%m/%d")

        row = {"date": date}
        for sign_idx, (sign_name, _, _) in enumerate(SIGNS):
            result = score_day_for_sign(date_str, sign_idx)
            row[f"{sign_name}_score"] = result["score"]
            row[f"{sign_name}_n_aspects"] = result["n_aspects"]
        records.append(row)

    return pd.DataFrame(records).set_index("date")


async def fetch_btc_daily(since_year: int = 2017) -> pd.DataFrame:
    """Fetch BTC/USDT daily candles from Binance."""
    exchange = ccxt.binance({"enableRateLimit": True})
    try:
        since = exchange.parse8601(f"{since_year}-01-01T00:00:00Z")
        all_candles = []
        while True:
            candles = await exchange.fetch_ohlcv(
                "BTC/USDT", "1d", since=since, limit=1000
            )
            if not candles:
                break
            all_candles.extend(candles)
            since = candles[-1][0] + 86400000
            if len(candles) < 1000:
                break

        df = pd.DataFrame(
            all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["date"] = pd.to_datetime(df["timestamp"], unit="ms")
        df = df.set_index("date")
        df["returns"] = df["close"].pct_change()
        df["abs_return"] = df["returns"].abs()
        return df
    finally:
        await exchange.close()


def analyze_sign(btc_df: pd.DataFrame, astro_df: pd.DataFrame,
                 sign_name: str) -> dict:
    """Analyze correlation between sign's astro score and BTC."""
    score_col = f"{sign_name}_score"

    merged = btc_df.join(astro_df[[score_col]], how="inner").dropna()
    if len(merged) < 100:
        return {}

    scores = merged[score_col]
    returns = merged["returns"]

    # Correlation
    corr, p_corr = stats.pearsonr(scores, returns)

    # Tercile analysis: bad / neutral / good days
    try:
        merged["regime"] = pd.qcut(scores, 3, labels=["bad", "neutral", "good"],
                                    duplicates="drop")
    except ValueError:
        # Not enough unique values for terciles, use manual splits
        q33 = scores.quantile(0.33)
        q66 = scores.quantile(0.66)
        merged["regime"] = "neutral"
        merged.loc[scores <= q33, "regime"] = "bad"
        merged.loc[scores >= q66, "regime"] = "good"

    regime_stats = {}
    for regime in ["bad", "neutral", "good"]:
        subset = merged[merged["regime"] == regime]
        if len(subset) > 10:
            regime_stats[regime] = {
                "n_days": len(subset),
                "mean_return": subset["returns"].mean(),
                "median_return": subset["returns"].median(),
                "positive_pct": (subset["returns"] > 0).mean(),
                "mean_vol": subset["abs_return"].mean(),
                "score_range": f"{subset[score_col].min():.1f} to {subset[score_col].max():.1f}",
            }

    # T-test: bad vs good days
    bad_days = merged[merged["regime"] == "bad"]["returns"]
    good_days = merged[merged["regime"] == "good"]["returns"]
    t_stat, p_value = 0, 1
    if len(bad_days) > 5 and len(good_days) > 5:
        t_stat, p_value = stats.ttest_ind(good_days, bad_days)

    # Predictive: does today's score predict TOMORROW's return?
    merged["next_return"] = merged["returns"].shift(-1)
    clean = merged.dropna()
    pred_corr, pred_p = stats.pearsonr(clean[score_col], clean["next_return"])

    return {
        "correlation": corr, "p_correlation": p_corr,
        "regimes": regime_stats,
        "t_stat_good_vs_bad": t_stat, "p_value_good_vs_bad": p_value,
        "predictive_corr": pred_corr, "predictive_p": pred_p,
        "n_days": len(merged),
    }


def print_report(all_results: dict):
    """Print formatted report."""
    print("=" * 75)
    print("  ZODIAC TRADING BACKTEST: Daily Astro Score vs BTC Returns")
    print("=" * 75)

    # Summary table
    print("\n-- All Signs Summary --")
    print(f"  {'Sign':<14s} {'Corr':>7s} {'p':>7s} {'Bad ret':>9s} {'Good ret':>9s} "
          f"{'t-stat':>7s} {'p(t)':>7s} {'Pred.r':>7s} {'Pred.p':>7s}")
    print("  " + "-" * 73)

    ranked = sorted(all_results.items(),
                    key=lambda x: abs(x[1].get("correlation", 0)), reverse=True)

    for sign, r in ranked:
        if not r:
            continue
        bad_ret = r["regimes"].get("bad", {}).get("mean_return", 0)
        good_ret = r["regimes"].get("good", {}).get("mean_return", 0)
        sig = "*" if r["p_correlation"] < 0.05 else " "
        pred_sig = "*" if r["predictive_p"] < 0.05 else " "
        marker = " <--" if sign in ("Cancer", "Pisces") else ""

        print(f"  {sign:<14s} {r['correlation']:+.4f} {r['p_correlation']:.4f}{sig}"
              f" {bad_ret*100:+.3f}% {good_ret*100:+.3f}% "
              f" {r['t_stat_good_vs_bad']:+.3f} {r['p_value_good_vs_bad']:.4f}"
              f" {r['predictive_corr']:+.4f} {r['predictive_p']:.4f}{pred_sig}{marker}")

    # Detailed view for Cancer and Pisces
    for focus_sign in ["Cancer", "Pisces"]:
        r = all_results.get(focus_sign, {})
        if not r:
            continue
        print(f"\n-- {focus_sign.upper()} Detail (most superstitious sign) --")
        print(f"  Days analyzed: {r['n_days']}")
        print(f"  Score-Return correlation: r={r['correlation']:+.4f}, p={r['p_correlation']:.4f}")
        print(f"  Predictive (score -> next day): r={r['predictive_corr']:+.4f}, p={r['predictive_p']:.4f}")

        for regime in ["bad", "neutral", "good"]:
            s = r["regimes"].get(regime, {})
            if s:
                print(f"  {regime.upper():>8s}: n={s['n_days']:4d}  "
                      f"mean={s['mean_return']*100:+.4f}%/d  "
                      f"median={s['median_return']*100:+.4f}%/d  "
                      f"pos={s['positive_pct']*100:.0f}%  "
                      f"vol={s['mean_vol']*100:.2f}%  "
                      f"scores={s['score_range']}")

        sig = "YES" if r["p_value_good_vs_bad"] < 0.05 else "NO"
        print(f"  Good vs Bad t-test: t={r['t_stat_good_vs_bad']:+.3f}, "
              f"p={r['p_value_good_vs_bad']:.4f} -> Significant: {sig}")

    # Best sign (if any)
    sig_signs = [(s, r) for s, r in ranked if r and r["p_correlation"] < 0.05]
    pred_signs = [(s, r) for s, r in ranked if r and r["predictive_p"] < 0.05]

    print("\n-- VERDICT --")
    if sig_signs:
        print(f"  Signs with significant same-day correlation:")
        for s, r in sig_signs:
            print(f"    {s}: r={r['correlation']:+.4f}, p={r['p_correlation']:.4f}")
    else:
        print("  No sign shows significant same-day correlation with BTC returns")

    if pred_signs:
        print(f"  Signs with PREDICTIVE power (score -> next day return):")
        for s, r in pred_signs:
            print(f"    {s}: r={r['predictive_corr']:+.4f}, p={r['predictive_p']:.4f}")
    else:
        print("  No sign shows predictive power for next-day BTC returns")

    print("=" * 75)


async def main():
    print("Fetching BTC/USDT daily data...")
    btc_df = await fetch_btc_daily(2017)
    print(f"  {len(btc_df)} daily candles")

    start = btc_df.index[0].strftime("%Y-%m-%d")
    end = btc_df.index[-1].strftime("%Y-%m-%d")

    print(f"\nCalculating daily astro scores for all 12 signs ({start} to {end})...")
    astro_df = calculate_all_days(start, end)
    print(f"  {len(astro_df)} days computed")

    # Save intermediate data
    output_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'reports')
    os.makedirs(output_dir, exist_ok=True)
    astro_df.to_csv(os.path.join(output_dir, "zodiac_daily_scores.csv"))

    print("\nAnalyzing each sign vs BTC returns...")
    all_results = {}
    for sign_idx, (sign_name, _, _) in enumerate(SIGNS):
        result = analyze_sign(btc_df, astro_df, sign_name)
        all_results[sign_name] = result

    print_report(all_results)

    # Bonus: combined "fear index" from most superstitious signs
    print("\n-- BONUS: Combined Fear Index (Cancer + Pisces + Scorpio) --")
    fear_cols = ["Cancer_score", "Pisces_score", "Scorpio_score"]
    astro_df["fear_index"] = astro_df[fear_cols].mean(axis=1)
    merged = btc_df.join(astro_df[["fear_index"]], how="inner").dropna()

    corr, p = stats.pearsonr(merged["fear_index"], merged["returns"])
    print(f"  Fear Index vs BTC return: r={corr:+.4f}, p={p:.4f}")

    # Quintiles of fear index
    merged["fear_q"] = pd.qcut(merged["fear_index"], 5, labels=False, duplicates="drop")
    print(f"  {'Q':>3s} {'N':>5s} {'MeanRet':>10s} {'Pos%':>6s} {'AvgScore':>10s}")
    for q in sorted(merged["fear_q"].unique()):
        sub = merged[merged["fear_q"] == q]
        print(f"  {q:3.0f} {len(sub):5d} {sub['returns'].mean()*100:+8.4f}% "
              f"{(sub['returns']>0).mean()*100:5.1f}% {sub['fear_index'].mean():+8.2f}")

    print("=" * 75)


if __name__ == "__main__":
    asyncio.run(main())
