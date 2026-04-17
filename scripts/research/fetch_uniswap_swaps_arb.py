"""
Скачать swap events из Uniswap v3 WETH/USDC pool на Arbitrum One через public RPC.

Выход: data/raw/onchain/uniswap_v3_arb_weth_usdc_{date}.parquet
    columns: block, ts_sec, eth_price_usdc, amount_usdc, amount_eth, direction

Пример:
    python scripts/research/fetch_uniswap_swaps_arb.py --date 2026-03-01
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

# Uniswap v3 WETH/USDC.e 0.05% pool on Arbitrum One
# WETH = 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1 (token0, 18 dec)
# USDC.e = 0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8 (token1, 6 dec)
POOL = "0xC31E54c7a869B9FcBEcc14363CF510d1c41fa443"
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

# On Arbitrum: WETH=token0 (18 dec), USDC.e=token1 (6 dec)
# Price from sqrtPriceX96: price = (sqrtPriceX96 / 2^96)^2 = token1/token0 in raw units
# = (USDC.e_raw / WETH_raw) → need to adjust decimals: * 10^(18-6) = * 10^12
# So eth_price_usdc = (sqrtPriceX96 / 2^96)^2 * 10^12
DECIMALS_DIFF = 18 - 6  # WETH decimals - USDC decimals

RPCS = [
    "https://arbitrum-one-rpc.publicnode.com",
    "https://1rpc.io/arb",
    "https://arbitrum.drpc.org",
    "https://rpc.ankr.com/arbitrum",
]

OUT = Path("data/raw/onchain")
BATCH_BLOCKS = 50_000  # Arbitrum blocks are ~0.25s, so 50k blocks ≈ 3.5 hours
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
    """Бинарный поиск блока по timestamp на Arbitrum."""
    latest = int(rpc_call("eth_blockNumber", []), 16)
    latest_data = rpc_call("eth_getBlockByNumber", [hex(latest), False])
    latest_ts = int(latest_data["timestamp"], 16)

    # Arbitrum: ~4 blocks/sec, so ~0.25s/block
    est = latest - int((latest_ts - target_ts) * 4)
    est = max(0, est)

    # Refine with binary search
    for _ in range(5):
        try:
            est_data = rpc_call("eth_getBlockByNumber", [hex(est), False])
            if est_data is None:
                est = est - 10_000
                continue
            est_ts = int(est_data["timestamp"], 16)
            diff = target_ts - est_ts
            if abs(diff) < 2:
                break
            est += int(diff * 4)  # 4 blocks per second
            est = max(0, est)
        except Exception:
            est = est - 10_000
        time.sleep(RATE_LIMIT_SEC)

    return est


def decode_swap(log: dict) -> dict:
    """Decode Uniswap v3 Swap event.

    On Arbitrum WETH/USDC.e pool:
        token0 = WETH (18 dec), token1 = USDC.e (6 dec)
        amount0 = WETH delta, amount1 = USDC.e delta
        sqrtPriceX96 → price = (sqrt/2^96)^2 * 10^(18-6) = ETH price in USDC
    """
    data = log["data"][2:]
    amount0 = int(data[0:64], 16)
    if amount0 > 2**255:
        amount0 -= 2**256
    amount1 = int(data[64:128], 16)
    if amount1 > 2**255:
        amount1 -= 2**256
    sqrtPriceX96 = int(data[128:192], 16)

    # price = (sqrtPriceX96 / 2^96)^2 gives token1/token0 in raw units
    # = USDC_raw / WETH_raw
    # To get human-readable: multiply by 10^(token0_dec - token1_dec) = 10^12
    price_raw = (sqrtPriceX96 / (2**96)) ** 2
    eth_price = price_raw * 10**DECIMALS_DIFF

    amount_eth = abs(amount0) / 1e18
    amount_usdc = abs(amount1) / 1e6

    # direction: если amount0 < 0 → pool sent WETH → юзер купил ETH → buy
    direction = "buy" if amount0 < 0 else "sell"

    return {
        "block": int(log["blockNumber"], 16),
        "eth_price_usdc": eth_price,
        "amount_usdc": amount_usdc,
        "amount_eth": amount_eth,
        "direction": direction,
    }


def fetch_block_timestamps(blocks: list[int]) -> dict[int, int]:
    """Получить timestamps через sparse sampling + linear interpolation.

    На Arbitrum блоков слишком много (6000+). Fetch каждый 50-й,
    interpolate остальные. Timestamps монотонные.
    """
    ts_map: dict[int, int] = {}
    unique = sorted(set(blocks))

    if len(unique) <= 200:
        # Small enough to fetch all via batch
        return _fetch_all_timestamps_batch(unique)

    # Sparse sample: every Nth block + first + last
    N = max(1, len(unique) // 100)  # ~100 sample points
    sample_indices = list(range(0, len(unique), N))
    if sample_indices[-1] != len(unique) - 1:
        sample_indices.append(len(unique) - 1)
    sample_blocks = [unique[i] for i in sample_indices]

    print(f"  Sparse sampling: {len(sample_blocks)} blocks out of {len(unique)}")
    sample_ts = _fetch_all_timestamps_batch(sample_blocks)

    # Interpolate
    import numpy as np
    known_blocks = sorted(sample_ts.keys())
    known_ts = [sample_ts[b] for b in known_blocks]

    for b in unique:
        if b in sample_ts:
            ts_map[b] = sample_ts[b]
        else:
            # Linear interpolation
            ts_map[b] = int(np.interp(b, known_blocks, known_ts))

    sys.stdout.write(f"\r  block timestamps: {len(unique)}/{len(unique)} done (interpolated)\n")
    return ts_map


def _fetch_all_timestamps_batch(blocks: list[int]) -> dict[int, int]:
    """Fetch timestamps via batch JSON-RPC."""
    ts_map: dict[int, int] = {}
    BATCH_SIZE = 50

    for batch_start in range(0, len(blocks), BATCH_SIZE):
        batch = blocks[batch_start:batch_start + BATCH_SIZE]
        payload = json.dumps([
            {"jsonrpc": "2.0", "method": "eth_getBlockByNumber",
             "params": [hex(b), False], "id": i}
            for i, b in enumerate(batch)
        ]).encode()

        rpc_idx = batch_start // BATCH_SIZE
        rpc = RPCS[rpc_idx % len(RPCS)]
        req = urllib.request.Request(rpc, payload, headers={
            "Content-Type": "application/json", "User-Agent": "research"
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                results = json.loads(r.read())
            for res, block_num in zip(results, batch):
                if "result" in res and res["result"] is not None:
                    ts_map[block_num] = int(res["result"]["timestamp"], 16)
        except Exception:
            # Fallback one-by-one
            for b in batch:
                try:
                    data = rpc_call("eth_getBlockByNumber", [hex(b), False], rpc_idx=rpc_idx)
                    ts_map[b] = int(data["timestamp"], 16)
                except Exception:
                    pass
                time.sleep(0.1)

        done = min(batch_start + BATCH_SIZE, len(blocks))
        sys.stdout.write(f"\r  block timestamps: {done}/{len(blocks)}")
        sys.stdout.flush()
        time.sleep(RATE_LIMIT_SEC)

    sys.stdout.write(f"\r  block timestamps: {len(blocks)}/{len(blocks)} done\n")
    return ts_map


def fetch_swaps(from_block: int, to_block: int) -> list[dict]:
    """Fetch all Swap events in block range."""
    all_swaps = []
    cur = from_block
    batch_idx = 0
    while cur <= to_block:
        end = min(cur + BATCH_BLOCKS - 1, to_block)
        for attempt in range(3):
            try:
                logs = rpc_call("eth_getLogs", [{
                    "fromBlock": hex(cur),
                    "toBlock": hex(end),
                    "address": POOL,
                    "topics": [SWAP_TOPIC],
                }], rpc_idx=batch_idx + attempt)
                break
            except Exception as e:
                if attempt < 2:
                    # Maybe batch too large, split in half
                    if "exceed" in str(e).lower() or "limit" in str(e).lower():
                        end = cur + (end - cur) // 2
                        print(f"\n  Reducing batch to {end - cur} blocks...")
                    time.sleep(2)
                else:
                    print(f"\n  batch {cur}-{end} failed: {e}, skipping")
                    logs = []

        for log in logs:
            try:
                all_swaps.append(decode_swap(log))
            except Exception:
                pass

        sys.stdout.write(
            f"\r  swaps: blocks {cur:,}-{end:,}, total {len(all_swaps)} swaps     "
        )
        sys.stdout.flush()
        cur = end + 1
        batch_idx += 1
        time.sleep(RATE_LIMIT_SEC)
    sys.stdout.write("\n")
    return all_swaps


def run(date: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / f"uniswap_v3_arb_weth_usdc_{date}.parquet"
    if out_path.exists():
        print(f"Already done: {out_path}")
        return

    from datetime import datetime, timezone
    dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ts = int(dt.timestamp())
    end_ts = start_ts + 86400

    print(f"=== Uniswap v3 WETH/USDC.e swaps on ARBITRUM for {date} ===")

    print("Finding block range...")
    from_block = find_block_at_timestamp(start_ts)
    to_block = find_block_at_timestamp(end_ts)
    print(f"  blocks: {from_block:,} .. {to_block:,} ({to_block - from_block:,} blocks)")

    print("Fetching swap events...")
    swaps = fetch_swaps(from_block, to_block)
    if not swaps:
        print("  No swaps found!")
        return
    print(f"  Total: {len(swaps)} swaps")

    # Get block timestamps (sample if too many unique blocks)
    blocks = list(set(s["block"] for s in swaps))
    if len(blocks) > 3000:
        # Too many unique blocks — get timestamps for all (will be slow)
        print(f"Fetching block timestamps ({len(blocks)} unique blocks)...")
        print("  (this may take a while on Arbitrum due to fast block times)")
    else:
        print(f"Fetching block timestamps ({len(blocks)} unique blocks)...")
    ts_map = fetch_block_timestamps(blocks)

    df = pd.DataFrame(swaps)
    df["ts_sec"] = df["block"].map(ts_map)
    df = df.dropna(subset=["ts_sec"])
    df["ts_sec"] = df["ts_sec"].astype(int)
    df = df.sort_values("ts_sec").reset_index(drop=True)

    # Sanity check: price should be ~$1900-2100 range for March 2026
    median_price = df["eth_price_usdc"].median()
    print(f"  Price sanity check: median=${median_price:.2f}")
    if median_price < 100 or median_price > 100_000:
        print(f"  WARNING: Price looks wrong! Check token0/token1 order.")
        # Try inverse
        df["eth_price_usdc"] = 1 / df["eth_price_usdc"] * 1e12
        print(f"  After inversion: median=${df['eth_price_usdc'].median():.2f}")

    df.to_parquet(out_path, index=False)
    print(f"\nSaved: {out_path} ({len(df):,} swaps, {out_path.stat().st_size / 1e6:.1f} MB)")
    print(f"  Price range: ${df['eth_price_usdc'].min():.2f} .. ${df['eth_price_usdc'].max():.2f}")
    print(f"  Buys: {(df['direction']=='buy').sum()}, Sells: {(df['direction']=='sell').sum()}")


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = ap.parse_args()
    run(args.date)


if __name__ == "__main__":
    cli()
