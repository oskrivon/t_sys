"""
Tick liquidity depth check: реальный slippage в Uniswap v3 pool.

Читает on-chain state пула (liquidity, sqrtPriceX96, tick, tickSpacing)
и симулирует swap для разных размеров: $1k, $5k, $10k, $25k.

Показывает: expected slippage в bps, effective price, available depth.

Пример:
    python scripts/research/check_pool_slippage.py
"""
from __future__ import annotations

import json
import math
import urllib.request
from pathlib import Path

REPORTS = Path("data/reports")

# Arbitrum WETH/USDC.e 0.05% pool
POOL = "0xC31E54c7a869B9FcBEcc14363CF510d1c41fa443"
# WETH = token0 (18 dec), USDC.e = token1 (6 dec)

RPC = "https://arbitrum-one-rpc.publicnode.com"

# Minimal ABIs
POOL_ABI = [
    {"inputs": [], "name": "slot0", "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"},
        {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"},
        {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"},
        {"name": "feeProtocol", "type": "uint8"},
        {"name": "unlocked", "type": "bool"},
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "liquidity", "outputs": [
        {"name": "", "type": "uint128"},
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "tickSpacing", "outputs": [
        {"name": "", "type": "int24"},
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tick", "type": "int24"}], "name": "ticks", "outputs": [
        {"name": "liquidityGross", "type": "uint128"},
        {"name": "liquidityNet", "type": "int128"},
        {"name": "feeGrowthOutside0X128", "type": "uint256"},
        {"name": "feeGrowthOutside1X128", "type": "uint256"},
        {"name": "tickCumulativeOutside", "type": "int56"},
        {"name": "secondsPerLiquidityOutsideX128", "type": "uint160"},
        {"name": "secondsOutside", "type": "uint32"},
        {"name": "initialized", "type": "bool"},
    ], "stateMutability": "view", "type": "function"},
]


def eth_call(to: str, data: str) -> str:
    """Raw eth_call."""
    payload = json.dumps({
        "jsonrpc": "2.0", "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"], "id": 1
    }).encode()
    req = urllib.request.Request(RPC, payload, headers={
        "Content-Type": "application/json", "User-Agent": "research"
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if "error" in resp:
        raise RuntimeError(f"RPC error: {resp['error']}")
    return resp["result"]


def call_slot0() -> dict:
    # slot0() selector = 0x3850c7bd
    result = eth_call(POOL, "0x3850c7bd")
    data = result[2:]
    sqrtPriceX96 = int(data[0:64], 16)
    tick_raw = int(data[64:128], 16)
    if tick_raw > 2**255:
        tick_raw -= 2**256
    return {"sqrtPriceX96": sqrtPriceX96, "tick": tick_raw}


def call_liquidity() -> int:
    result = eth_call(POOL, "0x1a686502")  # liquidity()
    return int(result, 16)


def call_tick_spacing() -> int:
    result = eth_call(POOL, "0xd0c93a7c")  # tickSpacing()
    return int(result, 16)


def call_ticks(tick: int) -> dict:
    """Query ticks(int24) for liquidityNet."""
    # ticks(int24) selector = 0xf30dba93
    # encode int24 as int256
    if tick < 0:
        tick_encoded = (1 << 256) + tick
    else:
        tick_encoded = tick
    data = "0xf30dba93" + f"{tick_encoded:064x}"
    result = eth_call(POOL, data)
    r = result[2:]
    liquidity_gross = int(r[0:64], 16)
    liquidity_net_raw = int(r[64:128], 16)
    if liquidity_net_raw > 2**127:
        liquidity_net_raw -= 2**128
    initialized = int(r[7*64:8*64], 16) != 0
    return {
        "liquidityGross": liquidity_gross,
        "liquidityNet": liquidity_net_raw,
        "initialized": initialized,
    }


def tick_to_price(tick: int) -> float:
    """Convert tick to human-readable price (USDC per ETH)."""
    # price = 1.0001^tick * 10^(token0_dec - token1_dec)
    # token0=WETH(18), token1=USDC(6)
    return (1.0001 ** tick) * (10 ** 12)


def sqrt_price_to_price(sqrtPriceX96: int) -> float:
    """Convert sqrtPriceX96 to human-readable price."""
    price_raw = (sqrtPriceX96 / (2**96)) ** 2
    return price_raw * (10 ** 12)  # WETH(18) - USDC(6) = 12


def simulate_swap_sell_eth(
    amount_eth: float,
    sqrtPriceX96: int,
    current_tick: int,
    tick_spacing: int,
    current_liquidity: int,
    tick_data: dict[int, dict],
    max_ticks: int = 100,
) -> dict:
    """Simulate selling ETH (token0) into pool -> receive USDC (token1).

    zeroForOne swap. Uses Uniswap v3 math:
        dx = L * (1/sqrt_P_new - 1/sqrt_P)   [raw token0]
        dy = L * (sqrt_P - sqrt_P_new)        [raw token1]
    """
    dx_remaining = amount_eth * 1e18  # raw WETH units
    dy_total = 0.0  # raw USDC.e units accumulated
    L = float(current_liquidity)
    sqrt_p = sqrtPriceX96 / (2**96)
    ticks_crossed = 0

    lower_tick = (current_tick // tick_spacing) * tick_spacing

    for _ in range(max_ticks):
        sqrt_p_lower = math.sqrt(1.0001 ** lower_tick)

        if L <= 0 or sqrt_p <= sqrt_p_lower:
            # Move to next tick range below
            lower_tick -= tick_spacing
            tick_info = tick_data.get(lower_tick + tick_spacing)
            if tick_info and tick_info["initialized"]:
                L -= tick_info["liquidityNet"]
            ticks_crossed += 1
            if lower_tick in tick_data:
                sqrt_p = sqrt_p_lower
            continue

        # Max dx absorbable: dx_max = L * (1/sqrt_p_lower - 1/sqrt_p)
        dx_max = L * (1.0 / sqrt_p_lower - 1.0 / sqrt_p)

        if dx_remaining <= dx_max:
            # Swap completes in this range
            # 1/sqrt_p_new = 1/sqrt_p + dx/L
            sqrt_p_new = 1.0 / (1.0 / sqrt_p + dx_remaining / L)
            dy = L * (sqrt_p - sqrt_p_new)
            dy_total += dy
            dx_remaining = 0
            break
        else:
            # Consume full tick range
            dy = L * (sqrt_p - sqrt_p_lower)
            dy_total += dy
            dx_remaining -= dx_max
            sqrt_p = sqrt_p_lower
            ticks_crossed += 1

            tick_info = tick_data.get(lower_tick)
            if tick_info and tick_info["initialized"]:
                L -= tick_info["liquidityNet"]
            lower_tick -= tick_spacing

    eth_sold = amount_eth - dx_remaining / 1e18
    # dy_total is in raw token1 units; USDC.e has 6 decimals
    usdc_received = dy_total / 1e6

    return {
        "eth_sold": eth_sold,
        "usdc_received": usdc_received,
        "avg_price": usdc_received / eth_sold if eth_sold > 0 else 0,
        "ticks_crossed": ticks_crossed,
        "fully_filled": dx_remaining < 1e10,
    }


def run() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)

    print("=== Uniswap v3 Pool Slippage Check ===")
    print(f"Pool: WETH/USDC.e 0.05% on Arbitrum")
    print(f"Address: {POOL}\n")

    # Read pool state
    print("Reading pool state...")
    slot0 = call_slot0()
    liquidity = call_liquidity()
    tick_spacing = call_tick_spacing()

    current_price = sqrt_price_to_price(slot0["sqrtPriceX96"])
    print(f"  Current price: ${current_price:.2f}")
    print(f"  Current tick: {slot0['tick']}")
    print(f"  Tick spacing: {tick_spacing}")
    print(f"  Current liquidity: {liquidity:,}")

    # Read tick data for nearby ticks (50 ticks each direction)
    print("\nReading nearby tick data...")
    tick_data: dict[int, dict] = {}
    base_tick = (slot0["tick"] // tick_spacing) * tick_spacing
    ticks_to_check = range(base_tick - 50 * tick_spacing, base_tick + 51 * tick_spacing, tick_spacing)

    initialized_count = 0
    for t in ticks_to_check:
        try:
            info = call_ticks(t)
            tick_data[t] = info
            if info["initialized"]:
                initialized_count += 1
        except Exception:
            pass

    print(f"  Checked {len(tick_data)} ticks, {initialized_count} initialized")

    # Simulate swaps at different sizes
    print("\n=== Slippage Simulation (Sell ETH -> USDC) ===\n")

    trade_sizes_usd = [500, 1_000, 5_000, 10_000, 25_000, 50_000]
    results = []

    for size_usd in trade_sizes_usd:
        amount_eth = size_usd / current_price

        swap = simulate_swap_sell_eth(
            amount_eth=amount_eth,
            sqrtPriceX96=slot0["sqrtPriceX96"],
            current_tick=slot0["tick"],
            tick_spacing=tick_spacing,
            current_liquidity=liquidity,
            tick_data=tick_data,
        )

        if swap["eth_sold"] > 0:
            slippage_bps = (current_price - swap["avg_price"]) / current_price * 10_000
            effective_size = swap["eth_sold"] * current_price
        else:
            slippage_bps = float("inf")
            effective_size = 0

        r = {
            "size_usd": size_usd,
            "amount_eth": amount_eth,
            "avg_price": swap["avg_price"],
            "slippage_bps": slippage_bps,
            "ticks_crossed": swap["ticks_crossed"],
            "filled": swap["fully_filled"],
        }
        results.append(r)

        print(f"  ${size_usd:>6,}: price=${swap['avg_price']:.2f}, "
              f"slippage={slippage_bps:+.2f} bps, "
              f"ticks={swap['ticks_crossed']}, filled={'YES' if swap['fully_filled'] else 'PARTIAL'}")

    # Impact on arb profitability
    print("\n=== Impact on Arb Profitability ===")
    print(f"  Median arb window net spread: 2.6 bps (from pool state analysis)")
    print(f"  Total fees: 15 bps (5 pool + 10 CEX)")
    print()
    for r in results:
        remaining = 2.6 - r["slippage_bps"]
        profit_usd = remaining / 10_000 * r["size_usd"]
        verdict = "PROFITABLE" if remaining > 0 else "UNPROFITABLE"
        print(f"  ${r['size_usd']:>6,}: slippage {r['slippage_bps']:+.2f} bps, "
              f"remaining {remaining:+.2f} bps -> ${profit_usd:+.2f}/trade -> {verdict}")

    # Write report
    lines = [
        "# Uniswap v3 Pool Slippage Check — Arbitrum WETH/USDC.e 0.05%",
        "",
        f"**Pool:** {POOL}",
        f"**Current price:** ${current_price:.2f}",
        f"**Current liquidity:** {liquidity:,}",
        f"**Tick spacing:** {tick_spacing}",
        f"**Initialized ticks nearby:** {initialized_count}",
        "",
        "## Slippage by trade size (sell ETH)",
        "",
        "| Trade size | ETH amount | Avg price | Slippage | Ticks crossed | Filled |",
        "|---:|---:|---:|---:|---:|---|",
    ]
    for r in results:
        lines.append(
            f"| ${r['size_usd']:,} | {r['amount_eth']:.4f} | "
            f"${r['avg_price']:.2f} | {r['slippage_bps']:+.2f} bps | "
            f"{r['ticks_crossed']} | {'Yes' if r['filled'] else 'Partial'} |"
        )

    lines += [
        "",
        "## Arb profitability after slippage",
        "",
        "Median arb window: 2.6 bps net (after 15 bps fees, before gas).",
        "",
        "| Trade size | Slippage | Remaining | Profit/trade | Verdict |",
        "|---:|---:|---:|---:|---|",
    ]
    for r in results:
        remaining = 2.6 - r["slippage_bps"]
        profit = remaining / 10_000 * r["size_usd"]
        verdict = "OK" if remaining > 0 else "DEAD"
        lines.append(
            f"| ${r['size_usd']:,} | {r['slippage_bps']:+.2f} bps | "
            f"{remaining:+.2f} bps | ${profit:+.2f} | **{verdict}** |"
        )

    lines += [
        "",
        "## Conclusion",
        "",
        f"If slippage at $5k is > 2.6 bps -> median arb windows are UNPROFITABLE.",
        f"Only p90+ windows (11.6 bps) survive if slippage < ~10 bps.",
        "",
    ]

    out = REPORTS / "pool_slippage_check.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {out}")


if __name__ == "__main__":
    run()
