"""
C1: Long-tail DEX пары на Arbitrum — pool state analysis.

Гипотеза: на alt/WETH пулах меньше ботов → спреды шире, окна длиннее.

Сравниваем с WETH/USDC baseline.

Пример:
    python scripts/research/alt_pools_arb.py --date 2026-03-01
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

RPCS = [
    "https://arbitrum-one-rpc.publicnode.com",
    "https://1rpc.io/arb",
    "https://arbitrum.drpc.org",
]

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

# Alt pools on Arbitrum One (Uniswap v3)
# Format: (pool_address, token0_symbol, token1_symbol, token0_dec, token1_dec, fee_bps, cex_symbol)
# NOTE: token order matters for price calculation
ALT_POOLS = [
    {
        "name": "ARB/WETH 0.05%",
        "pool": "0xC6F780497A95e246EB9449f5e4770916DCd6396A",
        "token0": "ARB",  # 0x912CE... (18 dec)
        "token1": "WETH",  # 0x82aF... (18 dec)
        "dec0": 18,
        "dec1": 18,
        "fee_bps": 5,
        "cex_pair": "ARB/USDT",  # need to convert via ETH price
        "price_is_inverse": True,  # we want ARB/USD, pool gives WETH/ARB
    },
    {
        "name": "ARB/USDC 0.05%",
        "pool": "0xb0f6cAA1630c5280159A572E7c3442f08a4d40B0",
        "token0": "ARB",  # 18 dec
        "token1": "USDC",  # 6 dec
        "dec0": 18,
        "dec1": 6,
        "fee_bps": 5,
        "cex_pair": "ARB/USDT",
        "price_is_inverse": False,  # pool gives USDC/ARB = ARB price in USDC
    },
    {
        "name": "WETH/USDC 0.05% (baseline)",
        "pool": "0xC31E54c7a869B9FcBEcc14363CF510d1c41fa443",
        "token0": "WETH",  # 18 dec
        "token1": "USDC.e",  # 6 dec
        "dec0": 18,
        "dec1": 6,
        "fee_bps": 5,
        "cex_pair": "ETH/USDT",
        "price_is_inverse": False,
    },
    {
        "name": "GMX/WETH 0.3%",
        "pool": "0x1aEEdD3727A6431b8F070C0aFaA81Cc74f273882",
        "token0": "GMX",  # 18 dec
        "token1": "WETH",  # 18 dec
        "dec0": 18,
        "dec1": 18,
        "fee_bps": 30,
        "cex_pair": "GMX/USDT",
        "price_is_inverse": True,
    },
    {
        "name": "LINK/WETH 0.3%",
        "pool": "0x468b88941e7Cc0B88c1869d68ab6b570bCEF62Ff",
        "token0": "LINK",  # 18 dec
        "token1": "WETH",  # 18 dec
        "dec0": 18,
        "dec1": 18,
        "fee_bps": 30,
        "cex_pair": "LINK/USDT",
        "price_is_inverse": True,
    },
]

OUT = Path("data/raw/onchain")
REPORTS = Path("data/reports")
BATCH_BLOCKS = 50_000
RATE_LIMIT_SEC = 0.5


def rpc_call(method: str, params: list, rpc_idx: int = 0) -> dict | list:
    rpc = RPCS[rpc_idx % len(RPCS)]
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(rpc, payload, headers={
        "Content-Type": "application/json", "User-Agent": "research"
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    if "error" in resp:
        raise RuntimeError(f"RPC error: {resp['error']}")
    return resp["result"]


def find_block_at_timestamp(target_ts: int) -> int:
    latest = int(rpc_call("eth_blockNumber", []), 16)
    latest_data = rpc_call("eth_getBlockByNumber", [hex(latest), False])
    latest_ts = int(latest_data["timestamp"], 16)
    est = latest - int((latest_ts - target_ts) * 4)
    est = max(0, est)
    for _ in range(5):
        try:
            est_data = rpc_call("eth_getBlockByNumber", [hex(est), False])
            if est_data is None:
                est -= 10_000
                continue
            est_ts = int(est_data["timestamp"], 16)
            diff = target_ts - est_ts
            if abs(diff) < 2:
                break
            est += int(diff * 4)
            est = max(0, est)
        except Exception:
            est -= 10_000
        time.sleep(RATE_LIMIT_SEC)
    return est


def fetch_swaps_for_pool(pool_addr: str, from_block: int, to_block: int) -> list[dict]:
    """Fetch Swap events for a specific pool."""
    all_logs = []
    cur = from_block
    batch_idx = 0
    while cur <= to_block:
        end = min(cur + BATCH_BLOCKS - 1, to_block)
        for attempt in range(3):
            try:
                logs = rpc_call("eth_getLogs", [{
                    "fromBlock": hex(cur),
                    "toBlock": hex(end),
                    "address": pool_addr,
                    "topics": [SWAP_TOPIC],
                }], rpc_idx=batch_idx + attempt)
                break
            except Exception as e:
                if attempt < 2 and ("exceed" in str(e).lower() or "limit" in str(e).lower()):
                    end = cur + (end - cur) // 2
                    time.sleep(2)
                elif attempt == 2:
                    logs = []
                else:
                    time.sleep(2)
        all_logs.extend(logs)
        sys.stdout.write(f"\r    blocks {cur:,}-{end:,}: {len(all_logs)} events")
        sys.stdout.flush()
        cur = end + 1
        batch_idx += 1
        time.sleep(RATE_LIMIT_SEC)
    sys.stdout.write("\n")
    return all_logs


def decode_swap_generic(log: dict, pool_cfg: dict) -> dict:
    """Decode swap event with configurable token decimals."""
    data = log["data"][2:]
    amount0 = int(data[0:64], 16)
    if amount0 > 2**255:
        amount0 -= 2**256
    amount1 = int(data[64:128], 16)
    if amount1 > 2**255:
        amount1 -= 2**256
    sqrtPriceX96 = int(data[128:192], 16)

    dec0 = pool_cfg["dec0"]
    dec1 = pool_cfg["dec1"]

    # sqrtPriceX96 gives sqrt(token1/token0) in raw units
    # price = (sqrtPriceX96 / 2^96)^2 = token1_raw / token0_raw
    # To get human-readable: * 10^(dec0 - dec1)
    price_raw = (sqrtPriceX96 / (2**96)) ** 2
    price = price_raw * 10**(dec0 - dec1)

    # price = token1 per token0 in human units
    # For token0/token1 pool (e.g., ARB/USDC): price = USDC per ARB = ARB price
    # For token0/token1 pool where token1=WETH (e.g., ARB/WETH): price = WETH per ARB

    return {
        "block": int(log["blockNumber"], 16),
        "price": price,
        "amount0": abs(amount0) / 10**dec0,
        "amount1": abs(amount1) / 10**dec1,
    }


def sparse_timestamps(blocks: list[int]) -> dict[int, int]:
    """Get timestamps via sparse sampling."""
    unique = sorted(set(blocks))
    if len(unique) <= 50:
        ts_map = {}
        for b in unique:
            try:
                d = rpc_call("eth_getBlockByNumber", [hex(b), False])
                ts_map[b] = int(d["timestamp"], 16)
            except Exception:
                pass
        return ts_map

    N = max(1, len(unique) // 50)
    sample_idx = list(range(0, len(unique), N))
    if sample_idx[-1] != len(unique) - 1:
        sample_idx.append(len(unique) - 1)
    sample = [unique[i] for i in sample_idx]

    # Batch fetch samples
    ts_map = {}
    BATCH = 50
    for i in range(0, len(sample), BATCH):
        batch = sample[i:i+BATCH]
        payload = json.dumps([
            {"jsonrpc": "2.0", "method": "eth_getBlockByNumber",
             "params": [hex(b), False], "id": j}
            for j, b in enumerate(batch)
        ]).encode()
        rpc = RPCS[(i // BATCH) % len(RPCS)]
        req = urllib.request.Request(rpc, payload, headers={
            "Content-Type": "application/json", "User-Agent": "research"
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                results = json.loads(r.read())
            for res, bn in zip(results, batch):
                if "result" in res and res["result"]:
                    ts_map[bn] = int(res["result"]["timestamp"], 16)
        except Exception:
            pass
        time.sleep(RATE_LIMIT_SEC)

    # Interpolate
    known = sorted(ts_map.keys())
    known_ts = [ts_map[b] for b in known]
    full_map = {}
    for b in unique:
        if b in ts_map:
            full_map[b] = ts_map[b]
        else:
            full_map[b] = int(np.interp(b, known, known_ts))
    return full_map


def analyze_pool(pool_cfg: dict, from_block: int, to_block: int, date: str) -> dict:
    """Fetch swaps and compute basic pool dynamics for one pool."""
    name = pool_cfg["name"]
    pool = pool_cfg["pool"]

    print(f"\n  [{name}] pool={pool[:10]}...")
    print(f"    Fetching swaps...")
    logs = fetch_swaps_for_pool(pool, from_block, to_block)

    if not logs:
        print(f"    No swaps found!")
        return {"name": name, "swaps": 0, "note": "no swaps"}

    # Decode
    swaps = []
    for log in logs:
        try:
            swaps.append(decode_swap_generic(log, pool_cfg))
        except Exception:
            pass

    if not swaps:
        return {"name": name, "swaps": 0, "note": "decode failed"}

    # Get timestamps
    blocks = [s["block"] for s in swaps]
    ts_map = sparse_timestamps(blocks)

    df = pd.DataFrame(swaps)
    df["ts_sec"] = df["block"].map(ts_map)
    df = df.dropna(subset=["ts_sec"])
    df["ts_sec"] = df["ts_sec"].astype(int)
    df = df.sort_values("ts_sec").reset_index(drop=True)

    n_swaps = len(df)
    if n_swaps == 0:
        return {"name": name, "swaps": 0, "note": "no timestamps"}

    # Gap analysis
    unique_secs = df.groupby("ts_sec")["price"].last().reset_index()
    gaps = unique_secs["ts_sec"].diff().dropna()

    # Pool state timeline
    pool_state = df.groupby("ts_sec")["price"].last().reset_index()
    pool_state.columns = ["ts_sec", "pool_price"]

    ts_min, ts_max = df["ts_sec"].min(), df["ts_sec"].max()
    timeline_len = ts_max - ts_min + 1

    result = {
        "name": name,
        "pool": pool,
        "fee_bps": pool_cfg["fee_bps"],
        "cex_pair": pool_cfg["cex_pair"],
        "swaps": n_swaps,
        "unique_seconds": len(unique_secs),
        "timeline_seconds": timeline_len,
        "pct_active": len(unique_secs) / timeline_len * 100 if timeline_len > 0 else 0,
        "gap_median": gaps.median() if len(gaps) > 0 else 0,
        "gap_p90": gaps.quantile(0.90) if len(gaps) > 0 else 0,
        "gap_max": gaps.max() if len(gaps) > 0 else 0,
        "price_median": df["price"].median(),
    }

    print(f"    Swaps: {n_swaps:,}, unique seconds: {len(unique_secs):,}")
    print(f"    Gaps: median={result['gap_median']:.0f}s, p90={result['gap_p90']:.0f}s, max={result['gap_max']:.0f}s")
    print(f"    Price (pool units): median={result['price_median']:.6f}")
    print(f"    Active: {result['pct_active']:.1f}% of seconds have swaps")

    return result


def run(date: str) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)

    from datetime import datetime, timezone
    dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ts = int(dt.timestamp())
    end_ts = start_ts + 86400

    print(f"=== Alt Pool Analysis on Arbitrum — {date} ===")
    print("Finding block range...")
    from_block = find_block_at_timestamp(start_ts)
    to_block = find_block_at_timestamp(end_ts)
    print(f"  blocks: {from_block:,} .. {to_block:,}")

    results = []
    for pool_cfg in ALT_POOLS:
        r = analyze_pool(pool_cfg, from_block, to_block, date)
        results.append(r)

    # Report
    lines = [
        f"# Alt Pool Dynamics on Arbitrum — {date}",
        "",
        "**Hypothesis:** Alt/WETH pools have less bot competition, wider spreads, longer windows.",
        "",
        "## Pool Dynamics Comparison",
        "",
        "| Pool | Fee | Swaps | Active % | Gap med | Gap p90 | Gap max |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        if r["swaps"] == 0:
            lines.append(f"| {r['name']} | - | 0 | - | - | - | - |")
            continue
        lines.append(
            f"| {r['name']} | {r.get('fee_bps',0)} bps | {r['swaps']:,} | "
            f"{r['pct_active']:.1f}% | {r['gap_median']:.0f}s | "
            f"{r['gap_p90']:.0f}s | {r['gap_max']:.0f}s |"
        )

    lines += [
        "",
        "## Key Observations",
        "",
        "Compare with WETH/USDC baseline (9,030 swaps, gap median=3s, p90=39s).",
        "",
        "**More swaps + shorter gaps = more bot activity = harder to arb.**",
        "**Fewer swaps + longer gaps = less competition = potential opportunity,**",
        "**BUT also means less liquidity = more slippage.**",
        "",
        "## Implications for Arbitrage",
        "",
        "- Pools with gap median >10s and fee <30 bps are candidates for slow-arb",
        "- 0.3% fee pools need >45 bps spread (30 pool + 10 CEX + buffer) to be profitable",
        "- 0.05% fee pools need >20 bps spread",
        "",
    ]

    out = REPORTS / f"alt_pools_arb_{date}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {out}")


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = ap.parse_args()
    run(args.date)


if __name__ == "__main__":
    cli()
