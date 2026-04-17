"""
Сопоставление CEX (Binance ETH/USDT L1) и DEX (Uniswap v3 WETH/USDC) цен.

Вход:
    data/raw/onchain/uniswap_v3_weth_usdc_{date}.parquet
    data/processed/arb_research/tardis_binance_ETHUSDT_{date}.parquet

Выход:
    data/reports/cex_dex_spread_{date}.md

Метрики:
    - Распределение spread (DEX execution price vs CEX mid/bid/ask)
    - Прибыльность slow-arb: spread > gas + pool_fee + cex_fee
    - Направленность (DEX дороже / дешевле)
    - Временной профиль
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ONCHAIN = Path("data/raw/onchain")
L1 = Path("data/processed/arb_research")
OUT = Path("data/reports")

# costs
UNISWAP_POOL_FEE_BPS = 5.0     # 0.05% pool
CEX_TAKER_FEE_BPS = 10.0       # Binance spot taker
GAS_COST_USD_L1 = 5.0           # conservative avg gas for L1 swap (~$3-10)
GAS_COST_USD_ARB = 0.10         # Arbitrum gas (~$0.05-0.20)


def run(date: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    dex_path = ONCHAIN / f"uniswap_v3_weth_usdc_{date}.parquet"
    cex_path = L1 / f"tardis_binance_ETHUSDT_{date}.parquet"

    if not dex_path.exists():
        print(f"Missing DEX data: {dex_path}")
        return
    if not cex_path.exists():
        print(f"Missing CEX data: {cex_path}")
        return

    dex = pd.read_parquet(dex_path)
    cex = pd.read_parquet(cex_path)

    # CEX: second_ms → second
    cex["ts_sec"] = cex["second_ms"] // 1000
    cex = cex.set_index("ts_sec").sort_index()

    # DEX: align each swap to nearest CEX second
    dex = dex.sort_values("ts_sec").reset_index(drop=True)

    # merge: for each DEX swap, find CEX state in that second
    merged = dex.merge(
        cex[["last_bid", "last_ask", "max_bid", "min_ask"]],
        left_on="ts_sec",
        right_index=True,
        how="inner",
    )
    print(f"DEX swaps: {len(dex)}, merged with CEX: {len(merged)}")

    if merged.empty:
        print("No overlapping data!")
        return

    # compute spreads
    # DEX price vs CEX mid
    merged["cex_mid"] = (merged["last_bid"] + merged["last_ask"]) / 2
    merged["spread_vs_mid_bps"] = (
        (merged["eth_price_usdc"] - merged["cex_mid"]) / merged["cex_mid"] * 10_000
    )

    # For arb profitability:
    # If DEX is CHEAPER → buy DEX, sell CEX → profit = CEX_bid - DEX_price - fees
    # If DEX is MORE EXPENSIVE → buy CEX, sell DEX → profit = DEX_price - CEX_ask - fees
    total_fee_bps = UNISWAP_POOL_FEE_BPS + CEX_TAKER_FEE_BPS  # 15 bps

    # Direction 1: buy DEX sell CEX (DEX cheaper)
    merged["arb_buy_dex_bps"] = (
        (merged["last_bid"] - merged["eth_price_usdc"]) / merged["eth_price_usdc"] * 10_000
        - total_fee_bps
    )
    # Direction 2: buy CEX sell DEX (DEX more expensive)
    merged["arb_sell_dex_bps"] = (
        (merged["eth_price_usdc"] - merged["last_ask"]) / merged["last_ask"] * 10_000
        - total_fee_bps
    )

    # Best direction per swap
    merged["best_arb_bps"] = merged[["arb_buy_dex_bps", "arb_sell_dex_bps"]].max(axis=1)
    merged["best_dir"] = merged.apply(
        lambda r: "buy_dex" if r["arb_buy_dex_bps"] > r["arb_sell_dex_bps"] else "sell_dex",
        axis=1,
    )

    # Gas check: is arb profitable after gas?
    # profit_usd = best_arb_bps / 10000 * trade_size
    # We need profit_usd > gas_cost
    # Min trade size for L1: gas_cost / (arb_bps/10000) = 5 / (arb_bps/10000)
    merged["min_trade_usd_l1"] = GAS_COST_USD_L1 / (merged["best_arb_bps"].clip(lower=0.01) / 10_000)
    merged["min_trade_usd_arb"] = GAS_COST_USD_ARB / (merged["best_arb_bps"].clip(lower=0.01) / 10_000)

    # profitable swaps (before gas)
    profitable_pre_gas = merged[merged["best_arb_bps"] > 0]
    # profitable at different trade sizes
    profitable_1k_l1 = profitable_pre_gas[profitable_pre_gas["min_trade_usd_l1"] < 1000]
    profitable_10k_l1 = profitable_pre_gas[profitable_pre_gas["min_trade_usd_l1"] < 10000]
    profitable_1k_arb = profitable_pre_gas[profitable_pre_gas["min_trade_usd_arb"] < 1000]

    # ── Stats ──
    spread_abs = merged["spread_vs_mid_bps"].abs()
    hours = pd.to_datetime(merged["ts_sec"], unit="s", utc=True).dt.hour

    lines = [
        f"# CEX-DEX spread analysis — {date}",
        "",
        f"**DEX:** Uniswap v3 WETH/USDC 0.05% (Ethereum L1)",
        f"**CEX:** Binance ETH/USDT (Tardis L1 bookTicker)",
        f"**Swaps analyzed:** {len(merged):,}",
        "",
        f"## Costs model",
        f"- Uniswap pool fee: {UNISWAP_POOL_FEE_BPS:.0f} bps",
        f"- CEX taker fee: {CEX_TAKER_FEE_BPS:.0f} bps",
        f"- Total fee (excl gas): **{total_fee_bps:.0f} bps**",
        f"- Gas L1 Ethereum: ~${GAS_COST_USD_L1}",
        f"- Gas Arbitrum: ~${GAS_COST_USD_ARB}",
        "",
        "## Spread distribution (DEX price vs CEX mid, bps)",
        f"- mean:   {merged['spread_vs_mid_bps'].mean():+.1f}",
        f"- median: {merged['spread_vs_mid_bps'].median():+.1f}",
        f"- std:    {merged['spread_vs_mid_bps'].std():.1f}",
        f"- p5:     {merged['spread_vs_mid_bps'].quantile(0.05):+.1f}",
        f"- p95:    {merged['spread_vs_mid_bps'].quantile(0.95):+.1f}",
        f"- min:    {merged['spread_vs_mid_bps'].min():+.1f}",
        f"- max:    {merged['spread_vs_mid_bps'].max():+.1f}",
        "",
        f"|Spread| > N bps:",
        f"- > 5 bps:  {(spread_abs > 5).sum():,} swaps ({(spread_abs > 5).mean()*100:.1f}%)",
        f"- > 10 bps: {(spread_abs > 10).sum():,} ({(spread_abs > 10).mean()*100:.1f}%)",
        f"- > 20 bps: {(spread_abs > 20).sum():,} ({(spread_abs > 20).mean()*100:.1f}%)",
        f"- > 50 bps: {(spread_abs > 50).sum():,} ({(spread_abs > 50).mean()*100:.1f}%)",
        f"- >100 bps: {(spread_abs > 100).sum():,} ({(spread_abs > 100).mean()*100:.1f}%)",
        "",
        f"## Арб profitability (после pool fee + CEX fee, ДО gas)",
        f"- **Profitable swaps (arb_bps > 0):** {len(profitable_pre_gas):,} / {len(merged):,} "
        f"({len(profitable_pre_gas)/len(merged)*100:.1f}%)",
        "",
    ]
    if not profitable_pre_gas.empty:
        lines += [
            f"Profitable arb bps distribution:",
            f"- median: {profitable_pre_gas['best_arb_bps'].median():.1f}",
            f"- p90:    {profitable_pre_gas['best_arb_bps'].quantile(0.9):.1f}",
            f"- max:    {profitable_pre_gas['best_arb_bps'].max():.1f}",
            "",
            f"Direction split:",
            f"- buy_dex (DEX дешевле): "
            f"{(profitable_pre_gas['best_dir']=='buy_dex').sum():,}",
            f"- sell_dex (DEX дороже): "
            f"{(profitable_pre_gas['best_dir']=='sell_dex').sum():,}",
            "",
            f"## После учёта gas",
            f"",
            f"### Ethereum L1 (gas ~${GAS_COST_USD_L1})",
            f"- Profitable при trade $1k: {len(profitable_1k_l1):,} swaps",
            f"- Profitable при trade $10k: {len(profitable_10k_l1):,} swaps",
            "",
            f"### Arbitrum (gas ~${GAS_COST_USD_ARB})",
            f"- Profitable при trade $1k: {len(profitable_1k_arb):,} swaps",
            "",
        ]

    # hour-of-day distribution for profitable
    if not profitable_pre_gas.empty:
        p_hours = pd.to_datetime(profitable_pre_gas["ts_sec"], unit="s", utc=True).dt.hour
        h_counts = p_hours.value_counts().sort_index()
        lines += [
            "## Profitable swaps по часам UTC",
            "```",
        ]
        for h in range(24):
            c = int(h_counts.get(h, 0))
            bar = "#" * min(40, c * 40 // max(1, int(h_counts.max()))) if h_counts.max() > 0 else ""
            lines.append(f"{h:2d}  {c:5d}  {bar}")
        lines += ["```", ""]

    out_path = OUT / f"cex_dex_spread_{date}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report: {out_path}")


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    args = ap.parse_args()
    run(args.date)


if __name__ == "__main__":
    cli()
