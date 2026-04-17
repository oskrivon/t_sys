"""
Pool state analysis: реальные CEX-DEX арб-окна МЕЖДУ свопами.

Логика:
  1. После каждого свопа Uniswap v3 фиксирует новую цену (sqrtPriceX96 → eth_price_usdc).
  2. Между свопами цена пула НЕИЗМЕННА → это реальная возможность для slow-arb.
  3. Строим per-second timeline: forward-fill pool price, сопоставляем с CEX mid.
  4. Арб-окно = непрерывный период, где |spread| > threshold (после вычета fees).

Вход:
    data/raw/onchain/uniswap_v3_{chain}_weth_usdc_{date}.parquet  (chain=eth или arb)
    data/processed/arb_research/tardis_binance_ETHUSDT_{date}.parquet

Выход:
    data/reports/pool_state_{chain}_{date}.md

Пример:
    python scripts/research/analyze_pool_state.py --date 2026-03-01
    python scripts/research/analyze_pool_state.py --date 2026-03-01 --chain arb
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import numpy as np

ONCHAIN = Path("data/raw/onchain")
L1 = Path("data/processed/arb_research")
OUT = Path("data/reports")

# Costs
UNISWAP_POOL_FEE_BPS = 5.0     # 0.05% pool
CEX_TAKER_FEE_BPS = 10.0       # Binance spot taker
TOTAL_FEE_BPS = UNISWAP_POOL_FEE_BPS + CEX_TAKER_FEE_BPS  # 15 bps

GAS_COST_USD = {
    "eth_l1": 5.0,
    "arbitrum": 0.10,
}
TRADE_SIZES_USD = [1_000, 5_000, 10_000, 25_000]


def build_pool_price_timeline(dex: pd.DataFrame) -> pd.DataFrame:
    """Строим per-second pool price через forward-fill.

    Для каждой секунды дня — цена пула после последнего свопа.
    Между свопами цена не меняется (AMM property).
    """
    # Если несколько свопов в одну секунду — берём последний (финальный pool state)
    dex_sorted = dex.sort_values(["ts_sec", "block"]).reset_index(drop=True)
    pool_state = dex_sorted.groupby("ts_sec").agg(
        pool_price=("eth_price_usdc", "last"),
        n_swaps=("eth_price_usdc", "count"),
        total_volume_usdc=("amount_usdc", "sum"),
    ).reset_index()

    # Полный per-second range
    ts_min = dex_sorted["ts_sec"].min()
    ts_max = dex_sorted["ts_sec"].max()
    full_range = pd.DataFrame({"ts_sec": range(ts_min, ts_max + 1)})

    # Merge и forward-fill
    timeline = full_range.merge(pool_state, on="ts_sec", how="left")
    timeline["pool_price"] = timeline["pool_price"].ffill()
    timeline["n_swaps"] = timeline["n_swaps"].fillna(0).astype(int)
    timeline["total_volume_usdc"] = timeline["total_volume_usdc"].fillna(0.0)

    # Отмечаем секунды, где произошёл своп (= конец текущего окна / начало нового)
    timeline["has_swap"] = timeline["n_swaps"] > 0

    # Время с последнего свопа (seconds since last swap)
    swap_indices = timeline.index[timeline["has_swap"]]
    timeline["sec_since_swap"] = 0
    for i in range(len(timeline)):
        if timeline.loc[i, "has_swap"]:
            timeline.loc[i, "sec_since_swap"] = 0
        elif i > 0:
            timeline.loc[i, "sec_since_swap"] = timeline.loc[i - 1, "sec_since_swap"] + 1

    return timeline


def find_arb_windows(
    timeline: pd.DataFrame,
    threshold_bps: float,
) -> pd.DataFrame:
    """Ищем непрерывные периоды, где |net_arb_bps| > 0 (profitable после fees).

    Арб-окно НАЧИНАЕТСЯ когда spread > threshold + fees
    и ЗАКАНЧИВАЕТСЯ когда происходит swap (кто-то забрал окно) или spread сходится.
    """
    # net_arb_bps = |spread| - total_fees - threshold
    mask = timeline["net_arb_bps"] > threshold_bps

    windows = []
    in_window = False
    start_idx = 0

    for i in range(len(timeline)):
        if mask.iloc[i] and not in_window:
            in_window = True
            start_idx = i
        elif not mask.iloc[i] and in_window:
            in_window = False
            windows.append(_extract_window(timeline, start_idx, i - 1))
        # Если был swap внутри окна — окно прерывается (новая цена)
        elif in_window and timeline.iloc[i]["has_swap"] and i > start_idx:
            windows.append(_extract_window(timeline, start_idx, i - 1))
            # Проверяем, продолжается ли окно после свопа
            if mask.iloc[i]:
                start_idx = i
            else:
                in_window = False

    # Закрываем последнее окно если open
    if in_window:
        windows.append(_extract_window(timeline, start_idx, len(timeline) - 1))

    if not windows:
        return pd.DataFrame()
    return pd.DataFrame(windows)


def _extract_window(tl: pd.DataFrame, start: int, end: int) -> dict:
    """Извлечь метрики одного арб-окна."""
    chunk = tl.iloc[start:end + 1]
    return {
        "start_ts": int(chunk["ts_sec"].iloc[0]),
        "end_ts": int(chunk["ts_sec"].iloc[-1]),
        "duration_sec": int(chunk["ts_sec"].iloc[-1] - chunk["ts_sec"].iloc[0] + 1),
        "max_spread_bps": float(chunk["spread_abs_bps"].max()),
        "mean_spread_bps": float(chunk["spread_abs_bps"].mean()),
        "max_net_arb_bps": float(chunk["net_arb_bps"].max()),
        "mean_net_arb_bps": float(chunk["net_arb_bps"].mean()),
        "direction": "buy_dex" if chunk["spread_bps"].mean() < 0 else "sell_dex",
        "ended_by_swap": bool(
            end + 1 < len(tl) and tl.iloc[end + 1]["has_swap"]
        ),
    }


def compute_economics(
    windows: pd.DataFrame,
    gas_cost_usd: float,
    trade_sizes: list[float],
) -> dict:
    """Для каждого trade_size считаем: сколько окон profitable после gas."""
    results = {}
    for size in trade_sizes:
        # profit_usd = net_arb_bps / 10000 * size
        # need profit_usd > gas_cost
        min_bps_for_gas = gas_cost_usd / size * 10_000
        profitable = windows[windows["max_net_arb_bps"] > min_bps_for_gas]
        if not profitable.empty:
            total_profit_bps = profitable["mean_net_arb_bps"].sum()
            total_profit_usd = total_profit_bps / 10_000 * size
            gas_total = len(profitable) * gas_cost_usd
            net_usd = total_profit_usd - gas_total
        else:
            total_profit_usd = 0.0
            gas_total = 0.0
            net_usd = 0.0

        results[size] = {
            "n_profitable": len(profitable),
            "total_profit_usd": total_profit_usd,
            "gas_total_usd": gas_total,
            "net_usd": net_usd,
        }
    return results


CHAIN_CONFIG = {
    "eth": {
        "dex_prefix": "uniswap_v3_weth_usdc",
        "label": "Ethereum L1",
        "block_time_sec": 12,
    },
    "arb": {
        "dex_prefix": "uniswap_v3_arb_weth_usdc",
        "label": "Arbitrum One",
        "block_time_sec": 0.25,
    },
}


def run(date: str, chain: str = "eth") -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    cfg = CHAIN_CONFIG[chain]
    dex_path = ONCHAIN / f"{cfg['dex_prefix']}_{date}.parquet"
    cex_path = L1 / f"tardis_binance_ETHUSDT_{date}.parquet"

    if not dex_path.exists():
        print(f"Missing DEX data: {dex_path}")
        return
    if not cex_path.exists():
        print(f"Missing CEX data: {cex_path}")
        return

    dex = pd.read_parquet(dex_path)
    cex = pd.read_parquet(cex_path)

    print(f"=== Pool State Analysis — {cfg['label']} — {date} ===")
    print(f"DEX swaps: {len(dex):,}")
    print(f"CEX seconds: {len(cex):,}")

    # 1. Build pool price timeline
    print("\nBuilding pool price timeline...")
    timeline = build_pool_price_timeline(dex)
    print(f"  Timeline: {len(timeline):,} seconds")
    print(f"  Seconds with swaps: {timeline['has_swap'].sum():,}")
    print(f"  Seconds without swaps: {(~timeline['has_swap']).sum():,}")

    # 2. Merge with CEX
    cex["ts_sec"] = cex["second_ms"] // 1000
    cex_indexed = cex.set_index("ts_sec")[["last_bid", "last_ask"]].sort_index()

    timeline = timeline.merge(
        cex_indexed, left_on="ts_sec", right_index=True, how="inner",
    )
    print(f"  After CEX merge: {len(timeline):,} seconds")

    # 3. Compute spreads
    timeline["cex_mid"] = (timeline["last_bid"] + timeline["last_ask"]) / 2
    timeline["spread_bps"] = (
        (timeline["pool_price"] - timeline["cex_mid"]) / timeline["cex_mid"] * 10_000
    )
    timeline["spread_abs_bps"] = timeline["spread_bps"].abs()

    # Net arb = |spread| - fees (before gas)
    timeline["net_arb_bps"] = timeline["spread_abs_bps"] - TOTAL_FEE_BPS

    # 4. Gap analysis
    dex_sorted = dex.sort_values("ts_sec")
    gaps = dex_sorted.groupby("ts_sec").first().reset_index()["ts_sec"].diff().dropna()

    # 5. Find arb windows (net_arb > 0 = profitable after fees, before gas)
    print("\nFinding arb windows...")
    windows = find_arb_windows(timeline, threshold_bps=0.0)

    if windows.empty:
        print("  No profitable windows found!")
        n_windows = 0
    else:
        n_windows = len(windows)
        print(f"  Windows found: {n_windows}")
        print(f"  Duration: median={windows['duration_sec'].median():.0f}s, "
              f"mean={windows['duration_sec'].mean():.1f}s, "
              f"max={windows['duration_sec'].max()}s")
        print(f"  Max net arb: median={windows['max_net_arb_bps'].median():.1f} bps, "
              f"p90={windows['max_net_arb_bps'].quantile(0.9):.1f} bps")

    # 6. Economics
    print("\n--- Economics ---")
    for network, gas in GAS_COST_USD.items():
        econ = compute_economics(windows, gas, TRADE_SIZES_USD)
        print(f"\n  {network} (gas ${gas}):")
        for size, r in econ.items():
            print(f"    ${size:>6,}: {r['n_profitable']:>4} windows, "
                  f"profit ${r['total_profit_usd']:>8,.1f}, "
                  f"gas ${r['gas_total_usd']:>6,.1f}, "
                  f"net ${r['net_usd']:>8,.1f}")

    # ── Generate report ──
    lines = _generate_report(date, chain, cfg, dex, timeline, gaps, windows, n_windows)
    out_path = OUT / f"pool_state_{chain}_{date}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {out_path}")


def _generate_report(
    date: str,
    chain: str,
    cfg: dict,
    dex: pd.DataFrame,
    timeline: pd.DataFrame,
    gaps: pd.Series,
    windows: pd.DataFrame,
    n_windows: int,
) -> list[str]:
    n_total_sec = len(timeline)
    n_swap_sec = int(timeline["has_swap"].sum())
    spread_profitable_sec = int((timeline["net_arb_bps"] > 0).sum())

    lines = [
        f"# Pool State CEX-DEX Arb Analysis — {cfg['label']} — {date}",
        "",
        f"**Методология:** Цена пула (sqrtPriceX96) МЕЖДУ свопами vs CEX mid.",
        f"В отличие от предыдущего анализа (execution price), здесь мы видим",
        f"**реальные незанятые окна** — period пока кто-то не сделает swap.",
        "",
        f"**Network:** {cfg['label']} (block time ~{cfg['block_time_sec']}s)",
        f"**DEX:** Uniswap v3 WETH/USDC 0.05%",
        f"**CEX:** Binance ETH/USDT (Tardis L1 bookTicker)",
        f"**Period:** {date}",
        "",
        "## Pool dynamics",
        f"- Total swaps: {len(dex):,}",
        f"- Unique seconds with swaps: {n_swap_sec:,}",
        f"- Total seconds in timeline: {n_total_sec:,}",
        f"- % времени без свопов: {(n_total_sec - n_swap_sec) / n_total_sec * 100:.1f}%",
        "",
        "### Gaps between swaps (seconds)",
        f"- mean:   {gaps.mean():.1f}",
        f"- median: {gaps.median():.0f}",
        f"- p75:    {gaps.quantile(0.75):.0f}",
        f"- p90:    {gaps.quantile(0.90):.0f}",
        f"- p95:    {gaps.quantile(0.95):.0f}",
        f"- max:    {gaps.max():.0f}",
        "",
        "## Spread distribution (pool price vs CEX mid, per-second)",
        f"- mean:   {timeline['spread_bps'].mean():+.2f} bps",
        f"- median: {timeline['spread_bps'].median():+.2f} bps",
        f"- std:    {timeline['spread_bps'].std():.2f} bps",
        f"- p5/p95: {timeline['spread_bps'].quantile(0.05):+.1f} / "
        f"{timeline['spread_bps'].quantile(0.95):+.1f} bps",
        "",
        f"|Spread| thresholds (per-second):",
        f"- > 5 bps:  {(timeline['spread_abs_bps'] > 5).sum():,} sec "
        f"({(timeline['spread_abs_bps'] > 5).mean()*100:.1f}%)",
        f"- > 10 bps: {(timeline['spread_abs_bps'] > 10).sum():,} "
        f"({(timeline['spread_abs_bps'] > 10).mean()*100:.1f}%)",
        f"- > 15 bps: {(timeline['spread_abs_bps'] > 15).sum():,} "
        f"({(timeline['spread_abs_bps'] > 15).mean()*100:.1f}%)",
        f"- > 20 bps: {(timeline['spread_abs_bps'] > 20).sum():,} "
        f"({(timeline['spread_abs_bps'] > 20).mean()*100:.1f}%)",
        f"- > 30 bps: {(timeline['spread_abs_bps'] > 30).sum():,} "
        f"({(timeline['spread_abs_bps'] > 30).mean()*100:.1f}%)",
        f"- > 50 bps: {(timeline['spread_abs_bps'] > 50).sum():,} "
        f"({(timeline['spread_abs_bps'] > 50).mean()*100:.1f}%)",
        "",
        f"## Profitable seconds (|spread| > {TOTAL_FEE_BPS:.0f} bps fees)",
        f"- Profitable seconds: **{spread_profitable_sec:,}** / {n_total_sec:,} "
        f"({spread_profitable_sec / n_total_sec * 100:.1f}%)",
        "",
    ]

    if n_windows > 0:
        lines += [
            f"## Арб-окна (непрерывные периоды net_arb > 0)",
            f"- **Всего окон: {n_windows:,}**",
            "",
            "### Duration distribution (seconds)",
            f"- mean:   {windows['duration_sec'].mean():.1f}",
            f"- median: {windows['duration_sec'].median():.0f}",
            f"- p75:    {windows['duration_sec'].quantile(0.75):.0f}",
            f"- p90:    {windows['duration_sec'].quantile(0.90):.0f}",
            f"- p95:    {windows['duration_sec'].quantile(0.95):.0f}",
            f"- max:    {windows['duration_sec'].max()}",
            "",
            "### Net arb size (bps, after fees before gas)",
            f"- max_net_arb mean:   {windows['max_net_arb_bps'].mean():.1f}",
            f"- max_net_arb median: {windows['max_net_arb_bps'].median():.1f}",
            f"- max_net_arb p90:    {windows['max_net_arb_bps'].quantile(0.9):.1f}",
            f"- max_net_arb max:    {windows['max_net_arb_bps'].max():.1f}",
            "",
            "### Direction",
            f"- buy_dex (DEX дешевле):  {(windows['direction']=='buy_dex').sum():,}",
            f"- sell_dex (DEX дороже):  {(windows['direction']=='sell_dex').sum():,}",
            "",
            "### Как заканчиваются окна",
            f"- Закрыто swap-ом (кто-то забрал): "
            f"{windows['ended_by_swap'].sum():,} ({windows['ended_by_swap'].mean()*100:.1f}%)",
            f"- Закрыто сходимостью CEX цены: "
            f"{(~windows['ended_by_swap']).sum():,} ({(~windows['ended_by_swap']).mean()*100:.1f}%)",
            "",
        ]

        # Economics tables
        for network, gas in GAS_COST_USD.items():
            econ = compute_economics(windows, gas, TRADE_SIZES_USD)
            lines += [
                f"## Экономика — {network} (gas ${gas})",
                "",
                "| Trade size | Profitable windows | Gross profit | Gas cost | Net profit |",
                "|---:|---:|---:|---:|---:|",
            ]
            for size, r in econ.items():
                lines.append(
                    f"| ${size:,} | {r['n_profitable']:,} | "
                    f"${r['total_profit_usd']:,.1f} | "
                    f"${r['gas_total_usd']:,.1f} | "
                    f"${r['net_usd']:,.1f} |"
                )
            lines.append("")

        # APR estimate
        lines += _apr_section(windows)

        # Hourly distribution
        lines += _hourly_section(windows)

    lines += [
        "## Важные caveats",
        "",
        "1. **Один день данных** — March 1, 2026. Нужна проверка на бо́льшем периоде.",
        "2. **Timing assumption** — мы видим pool price per-second, реально блок ~12s на L1.",
        "   На деле нужно ~1-2 блока чтобы swap прошёл, т.е. 12-24 секунды latency.",
        "3. **Slippage не учтён** — при большом trade size slippage в пуле может быть значительным.",
        "4. **MEV competition** — другие боты тоже видят эти окна. На L1 конкуренция жёсткая,",
        "   на Arbitrum (FIFO sequencer) конкуренция ниже.",
        "5. **Pool price = post-swap** — мы знаем цену после свопа, но не знаем что было",
        "   между блоками (цена могла кратковременно отклоняться сильнее).",
        "",
    ]

    return lines


def _apr_section(windows: pd.DataFrame) -> list[str]:
    """Estimate APR for different scenarios."""
    lines = [
        "## APR оценка (экстраполяция 1 день → год)",
        "",
        "**Допущения:** capture rate 30% (не все окна удастся исполнить),",
        "trading 365 дней/год.",
        "",
        "| Network | Trade size | Captured/day | Daily net | Monthly | APR |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    capture_rate = 0.30
    for network, gas in GAS_COST_USD.items():
        econ = compute_economics(windows, gas, TRADE_SIZES_USD)
        for size, r in econ.items():
            captured = int(r["n_profitable"] * capture_rate)
            if captured == 0:
                continue
            # Recalculate with capture rate
            profitable_windows = windows[
                windows["max_net_arb_bps"] > (gas / size * 10_000)
            ]
            if profitable_windows.empty:
                continue
            avg_profit_bps = profitable_windows["mean_net_arb_bps"].mean()
            daily_profit = captured * (avg_profit_bps / 10_000 * size) - captured * gas
            monthly = daily_profit * 30
            apr = daily_profit * 365 / size * 100
            lines.append(
                f"| {network} | ${size:,} | {captured} | "
                f"${daily_profit:,.1f} | ${monthly:,.0f} | "
                f"**{apr:.1f}%** |"
            )
    lines.append("")
    return lines


def _hourly_section(windows: pd.DataFrame) -> list[str]:
    """Distribution of windows by hour."""
    hours = pd.to_datetime(windows["start_ts"], unit="s", utc=True).dt.hour
    h_counts = hours.value_counts().sort_index()
    lines = [
        "## Окна по часам UTC",
        "```",
    ]
    max_count = h_counts.max() if not h_counts.empty else 1
    for h in range(24):
        c = int(h_counts.get(h, 0))
        bar = "#" * min(50, c * 50 // max(1, int(max_count))) if max_count > 0 else ""
        lines.append(f"{h:2d}  {c:4d}  {bar}")
    lines += ["```", ""]
    return lines


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--chain", default="eth", choices=["eth", "arb"],
                    help="Chain: eth (Ethereum L1) or arb (Arbitrum)")
    args = ap.parse_args()
    run(args.date, args.chain)


if __name__ == "__main__":
    cli()
