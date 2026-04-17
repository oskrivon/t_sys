"""
Gate 2 — сравнить trades-based bid/ask proxy и реальный L1 bookTicker на одном дне.

Прогоняем три детектора на одном и том же дне (по умолчанию 2026-03-01):
  A) PROXY    — bid_proxy/ask_proxy из trades (из data/processed/arb_research/{exch}_*.parquet)
  B) L1 OPT   — max_bid/min_ask из tick-level L1 (оптимистично — "был ли момент в секунду")
  C) L1 SNAP  — last_bid/last_ask из L1 (снэпшот в конце секунды)

Для каждого детектора считаем статистику (окна, длительность, bps).
Дополнительно — confusion matrix между PROXY и L1 OPT на уровне arb-секунд.

Вывод: data/reports/gate2_proxy_vs_l1_{date}.md
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

IN_DIR = Path("data/processed/arb_research")
OUT_DIR = Path("data/reports")

STALE_SEC = 30
FEE_BPS = 20.0

# (binance_symbol, bybit_symbol, okx_symbol, display)
PAIRS = [
    ("BTCUSDT", "BTCUSDT", "BTC-USDT", "BTC/USDT"),
    ("ETHUSDT", "ETHUSDT", "ETH-USDT", "ETH/USDT"),
]


def date_to_ms_range(date: str) -> tuple[int, int]:
    """YYYY-MM-DD -> [start_ms inclusive, end_ms exclusive)."""
    dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = int(dt.timestamp() * 1000)
    return start, start + 86_400_000


def load_proxy_for_day(
    exchange: str, symbol: str, month: str, start_ms: int, end_ms: int
) -> pd.DataFrame:
    path = IN_DIR / f"{exchange}_{symbol}_{month}.parquet"
    df = pd.read_parquet(path, columns=["second_ms", "bid_proxy", "ask_proxy"])
    df = df[(df["second_ms"] >= start_ms) & (df["second_ms"] < end_ms)]
    df = df.rename(columns={"bid_proxy": f"{exchange}_bid", "ask_proxy": f"{exchange}_ask"})
    return df.set_index("second_ms")


def load_l1_for_day(
    exchange: str, symbol: str, date: str, column_kind: str
) -> pd.DataFrame:
    """column_kind: 'opt' -> max_bid/min_ask, 'snap' -> last_bid/last_ask"""
    path = IN_DIR / f"tardis_{exchange}_{symbol}_{date}.parquet"
    if column_kind == "opt":
        cols = {"max_bid": f"{exchange}_bid", "min_ask": f"{exchange}_ask"}
    else:
        cols = {"last_bid": f"{exchange}_bid", "last_ask": f"{exchange}_ask"}
    df = pd.read_parquet(path, columns=["second_ms"] + list(cols.keys()))
    df = df.rename(columns=cols)
    return df.set_index("second_ms")


def build_wide(
    loader_fn, pair, **kwargs
) -> pd.DataFrame:
    """Склеить три биржи в wide-формат и заполнить пропуски ffill-ом."""
    bn_sym, by_sym, okx_sym, _ = pair
    binance = loader_fn("binance", bn_sym, **kwargs)
    bybit = loader_fn("bybit", by_sym, **kwargs)
    okx = loader_fn("okx", okx_sym, **kwargs)

    start = min(binance.index.min(), bybit.index.min(), okx.index.min())
    end = max(binance.index.max(), bybit.index.max(), okx.index.max())
    full = pd.RangeIndex(start=start, stop=end + 1000, step=1000, name="second_ms")

    df = pd.concat([binance, bybit, okx], axis=1).reindex(full)
    for col in df.columns:
        df[col] = df[col].ffill(limit=STALE_SEC)
    return df


def detect(df: pd.DataFrame) -> pd.DataFrame:
    bid_cols = [c for c in df.columns if c.endswith("_bid")]
    ask_cols = [c for c in df.columns if c.endswith("_ask")]
    bids = df[bid_cols]
    asks = df[ask_cols]

    has_bid = bids.notna().any(axis=1)
    has_ask = asks.notna().any(axis=1)
    mask_both = has_bid & has_ask

    best_bid = pd.Series(index=df.index, dtype="float64")
    best_bid_exch = pd.Series(index=df.index, dtype="object")
    best_ask = pd.Series(index=df.index, dtype="float64")
    best_ask_exch = pd.Series(index=df.index, dtype="object")

    if mask_both.any():
        vb = bids[mask_both]
        va = asks[mask_both]
        best_bid.loc[mask_both] = vb.max(axis=1)
        best_bid_exch.loc[mask_both] = vb.idxmax(axis=1).str.replace("_bid", "", regex=False)
        best_ask.loc[mask_both] = va.min(axis=1)
        best_ask_exch.loc[mask_both] = va.idxmin(axis=1).str.replace("_ask", "", regex=False)

    mask_diff = (best_bid_exch != best_ask_exch) & best_bid_exch.notna() & best_ask_exch.notna()
    gross_bps = (best_bid - best_ask) / best_ask * 10_000
    net_bps = gross_bps - FEE_BPS
    arb_mask = (mask_both & mask_diff & (net_bps > 0)).fillna(False)

    out = pd.DataFrame({
        "best_bid": best_bid,
        "best_ask": best_ask,
        "exch_sell": best_bid_exch,
        "exch_buy": best_ask_exch,
        "gross_bps": gross_bps,
        "net_bps": net_bps,
    })[arb_mask]
    return out


def compute_windows(arb: pd.DataFrame) -> pd.DataFrame:
    if arb.empty:
        return pd.DataFrame(columns=["start_ms","end_ms","duration_s","exch_sell","exch_buy","max_net_bps","median_net_bps"])
    direction = arb["exch_sell"] + ">" + arb["exch_buy"]
    ts = arb.index.to_series()
    gap = ts.diff().gt(1000) | direction.ne(direction.shift())
    wid = gap.cumsum()
    g = arb.assign(wid=wid.values).groupby("wid")
    out = g.agg(
        start_ms=("best_bid", lambda s: s.index.min()),
        end_ms=("best_bid", lambda s: s.index.max()),
        exch_sell=("exch_sell", "first"),
        exch_buy=("exch_buy", "first"),
        max_net_bps=("net_bps", "max"),
        median_net_bps=("net_bps", "median"),
    )
    out["duration_s"] = ((out["end_ms"] - out["start_ms"]) // 1000) + 1
    return out.reset_index(drop=True)


def stats_block(label: str, arb: pd.DataFrame, wins: pd.DataFrame) -> list[str]:
    lines = [f"**{label}**"]
    if arb.empty:
        lines.append("- окон не найдено")
        return lines
    lines += [
        f"- арб-секунд: {len(arb):,}",
        f"- окон: {len(wins):,}",
        f"- net_bps: median={arb['net_bps'].median():.2f}, p90={arb['net_bps'].quantile(0.9):.2f}, "
        f"p99={arb['net_bps'].quantile(0.99):.2f}, max={arb['net_bps'].max():.2f}",
        f"- длительность (s): median={wins['duration_s'].median():.0f}, "
        f"p90={wins['duration_s'].quantile(0.9):.0f}, max={wins['duration_s'].max():.0f}",
        f"- 1-сек окон: {(wins['duration_s']==1).sum():,} ({(wins['duration_s']==1).mean()*100:.1f}%)",
        f"- >=5с окон: {(wins['duration_s']>=5).sum():,}",
        f"- >=30с окон: {(wins['duration_s']>=30).sum():,}",
    ]
    return lines


def confusion_lines(
    proxy_arb: pd.DataFrame, l1_opt_arb: pd.DataFrame, total_sec: int
) -> list[str]:
    """Сравнить множества арб-секунд между proxy и L1 OPT."""
    p = set(proxy_arb.index)
    l = set(l1_opt_arb.index)
    tp = len(p & l)
    fp = len(p - l)  # proxy говорит арб, L1 говорит нет
    fn = len(l - p)  # proxy пропустил, L1 нашёл
    prec = tp / len(p) * 100 if p else 0.0
    rec = tp / len(l) * 100 if l else 0.0
    return [
        "### Confusion matrix: PROXY vs L1 OPT (на уровне arb-секунд)",
        f"- **True positives** (обе нашли): {tp:,}",
        f"- **False positives** (proxy нашёл, L1 нет): {fp:,}",
        f"- **False negatives** (L1 нашёл, proxy пропустил): {fn:,}",
        f"- Precision: {prec:.1f}%  (из тех что proxy пометил, сколько реально арб по L1)",
        f"- Recall: {rec:.1f}%  (из реальных L1-окон, сколько поймал proxy)",
        "",
    ]


def run(date: str) -> None:
    month = date[:7]
    start_ms, end_ms = date_to_ms_range(date)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = [
        f"# Gate 2: PROXY vs L1 — {date}",
        "",
        f"**Fee:** {FEE_BPS:.0f} bps round-trip",
        f"**Staleness:** {STALE_SEC}s",
        "",
        "## Определения",
        "- **PROXY** — `bid_proxy`/`ask_proxy` из trades (max sell-price / min buy-price в секунду)",
        "- **L1 OPT** — `max(bid)`/`min(ask)` за секунду (оптимистично: был ли момент)",
        "- **L1 SNAP** — `last(bid)`/`last(ask)` в конце секунды (снэпшот раз в секунду)",
        "",
        "PROXY теоретически — lower bound для L1 OPT (proxy видит только секунды со сделками).",
        "L1 SNAP показывает, что бы увидел процесс опрашивающий раз в секунду.",
        "",
    ]

    print(f"=== Gate 2 analysis, {date} ===")
    for pair in PAIRS:
        display = pair[3]
        print(f"\n-- {display} --")

        # PROXY на этом дне
        proxy_wide = build_wide(
            lambda exch, sym: load_proxy_for_day(exch, sym, month, start_ms, end_ms),
            pair,
        )
        proxy_arb = detect(proxy_wide)
        proxy_wins = compute_windows(proxy_arb)

        # L1 OPT
        l1opt_wide = build_wide(
            lambda exch, sym: load_l1_for_day(exch, sym, date, "opt"),
            pair,
        )
        l1opt_arb = detect(l1opt_wide)
        l1opt_wins = compute_windows(l1opt_arb)

        # L1 SNAP
        l1snap_wide = build_wide(
            lambda exch, sym: load_l1_for_day(exch, sym, date, "snap"),
            pair,
        )
        l1snap_arb = detect(l1snap_wide)
        l1snap_wins = compute_windows(l1snap_arb)

        print(f"  PROXY:   {len(proxy_arb):,} arb-sec, {len(proxy_wins):,} окон")
        print(f"  L1 OPT:  {len(l1opt_arb):,} arb-sec, {len(l1opt_wins):,} окон")
        print(f"  L1 SNAP: {len(l1snap_arb):,} arb-sec, {len(l1snap_wins):,} окон")

        report += [f"## {display}", ""]
        report += stats_block("PROXY (trades)", proxy_arb, proxy_wins)
        report.append("")
        report += stats_block("L1 OPT (max_bid / min_ask)", l1opt_arb, l1opt_wins)
        report.append("")
        report += stats_block("L1 SNAP (last_bid / last_ask)", l1snap_arb, l1snap_wins)
        report.append("")
        report += confusion_lines(proxy_arb, l1opt_arb, len(proxy_wide))

        # direction breakdown для L1 OPT
        if not l1opt_wins.empty:
            report += ["### Топ направлений (L1 OPT)"]
            directions = l1opt_wins.groupby(
                ["exch_sell", "exch_buy"]
            ).size().sort_values(ascending=False)
            for (s, b), cnt in directions.head(6).items():
                report.append(f"- `{s} -> {b}`: {cnt:,}")
            report.append("")

    report_path = OUT_DIR / f"gate2_proxy_vs_l1_{date}.md"
    report_path.write_text("\n".join(report), encoding="utf-8")
    print(f"\nReport: {report_path}")


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD, should match free-tier day")
    args = ap.parse_args()
    run(args.date)


if __name__ == "__main__":
    cli()
