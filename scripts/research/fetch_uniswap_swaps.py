"""
Скачать swap events из Uniswap v3 WETH/USDC pool через public RPC.

Выход: data/raw/onchain/uniswap_v3_weth_usdc_{date}.parquet
    columns: block, ts_sec, eth_price_usdc, amount_usdc, amount_eth, direction

Пример:
    python scripts/research/fetch_uniswap_swaps.py --date 2026-03-01
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

# Uniswap v3 WETH/USDC 0.05% pool on Ethereum mainnet
POOL = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

# USDC = token0 (6 decimals), WETH = token1 (18 decimals)
DECIMALS_DIFF = 18 - 6  # for price conversion

RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://1rpc.io/eth",
    "https://eth.drpc.org",
]

OUT = Path("data/raw/onchain")
BATCH_BLOCKS = 500  # blocks per eth_getLogs call
RATE_LIMIT_SEC = 0.3


def rpc_call(method: str, params: list, rpc_idx: int = 0) -> dict | list:
    rpc = RPCS[rpc_idx % len(RPCS)]
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(rpc, payload, headers={
        "Content-Type": "application/json", "User-Agent": "research"
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if "error" in resp:
        raise RuntimeError(f"RPC error: {resp['error']}")
    return resp["result"]


def find_block_at_timestamp(target_ts: int) -> int:
    """Бинарный поиск блока по timestamp."""
    latest = int(rpc_call("eth_blockNumber", []), 16)
    latest_data = rpc_call("eth_getBlockByNumber", [hex(latest), False])
    latest_ts = int(latest_data["timestamp"], 16)

    est = latest - int((latest_ts - target_ts) / 12.1)
    # refine с одним запросом
    est_data = rpc_call("eth_getBlockByNumber", [hex(est), False])
    est_ts = int(est_data["timestamp"], 16)
    est += int((target_ts - est_ts) / 12.1)
    return est


def decode_swap(log: dict) -> dict:
    data = log["data"][2:]
    amount0 = int(data[0:64], 16)
    if amount0 > 2**255:
        amount0 -= 2**256
    amount1 = int(data[64:128], 16)
    if amount1 > 2**255:
        amount1 -= 2**256
    sqrtPriceX96 = int(data[128:192], 16)

    price_raw = (sqrtPriceX96 / (2**96)) ** 2
    eth_price = (1 / price_raw) * 10**DECIMALS_DIFF if price_raw > 0 else 0.0

    amount_usdc = amount0 / 1e6
    amount_eth = amount1 / 1e18

    # direction: если amount1 > 0 → pool получил WETH → юзер продал ETH → sell
    direction = "sell" if amount1 > 0 else "buy"

    return {
        "block": int(log["blockNumber"], 16),
        "eth_price_usdc": eth_price,
        "amount_usdc": abs(amount_usdc),
        "amount_eth": abs(amount_eth),
        "direction": direction,
    }


def fetch_block_timestamps(blocks: list[int]) -> dict[int, int]:
    """Получить timestamps для списка блоков (batch)."""
    ts_map: dict[int, int] = {}
    unique = sorted(set(blocks))
    for i, b in enumerate(unique):
        try:
            data = rpc_call("eth_getBlockByNumber", [hex(b), False], rpc_idx=i)
            ts_map[b] = int(data["timestamp"], 16)
        except Exception:
            pass
        if (i + 1) % 10 == 0:
            sys.stdout.write(f"\r  block timestamps: {i+1}/{len(unique)}")
            sys.stdout.flush()
            time.sleep(RATE_LIMIT_SEC)
    sys.stdout.write(f"\r  block timestamps: {len(unique)}/{len(unique)} done\n")
    return ts_map


def fetch_swaps(from_block: int, to_block: int) -> list[dict]:
    """Fetch all Swap events in block range via eth_getLogs in batches."""
    all_swaps = []
    cur = from_block
    batch_idx = 0
    while cur <= to_block:
        end = min(cur + BATCH_BLOCKS - 1, to_block)
        try:
            logs = rpc_call("eth_getLogs", [{
                "fromBlock": hex(cur),
                "toBlock": hex(end),
                "address": POOL,
                "topics": [SWAP_TOPIC],
            }], rpc_idx=batch_idx)
        except Exception as e:
            print(f"  batch {cur}-{end} error: {e}, retrying...")
            time.sleep(2)
            try:
                logs = rpc_call("eth_getLogs", [{
                    "fromBlock": hex(cur),
                    "toBlock": hex(end),
                    "address": POOL,
                    "topics": [SWAP_TOPIC],
                }], rpc_idx=batch_idx + 1)
            except Exception as e2:
                print(f"  retry failed: {e2}, skipping")
                cur = end + 1
                batch_idx += 1
                continue
        for log in logs:
            try:
                all_swaps.append(decode_swap(log))
            except Exception:
                pass
        sys.stdout.write(
            f"\r  swaps: blocks {cur}-{end}, total {len(all_swaps)} swaps     "
        )
        sys.stdout.flush()
        cur = end + 1
        batch_idx += 1
        time.sleep(RATE_LIMIT_SEC)
    sys.stdout.write("\n")
    return all_swaps


def run(date: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / f"uniswap_v3_weth_usdc_{date}.parquet"
    if out_path.exists():
        print(f"Already done: {out_path}")
        return

    # parse date
    from datetime import datetime, timezone
    dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ts = int(dt.timestamp())
    end_ts = start_ts + 86400

    print(f"=== Uniswap v3 WETH/USDC swaps for {date} ===")

    # find block range
    print("Finding block range...")
    from_block = find_block_at_timestamp(start_ts)
    to_block = find_block_at_timestamp(end_ts)
    print(f"  blocks: {from_block} .. {to_block} ({to_block - from_block} blocks)")

    # fetch swaps
    print("Fetching swap events...")
    swaps = fetch_swaps(from_block, to_block)
    if not swaps:
        print("  No swaps found!")
        return
    print(f"  Total: {len(swaps)} swaps")

    # get block timestamps
    print("Fetching block timestamps...")
    blocks = [s["block"] for s in swaps]
    ts_map = fetch_block_timestamps(blocks)

    # build dataframe
    df = pd.DataFrame(swaps)
    df["ts_sec"] = df["block"].map(ts_map)
    df = df.dropna(subset=["ts_sec"])
    df["ts_sec"] = df["ts_sec"].astype(int)
    df = df.sort_values("ts_sec").reset_index(drop=True)

    # save
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
