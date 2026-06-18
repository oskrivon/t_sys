"""Icebreaker Этап 0 — cross-exchange symbol overlap pre-flight.

Answers the go/no-go question before any data collection: how many illiquid
USDT-perps are tradable on the execution venue (Bybit) AND watchable on a
detect venue (MEXC / KuCoin)? If few, the "enter on Bybit" premise is dead.

Reuses the universal CCXTAdapter (public, market-data only). Writes a seed
watchlist to config/icebreaker_watchlist.json.

Usage:
    python scripts/research/icebreaker_preflight.py
    python scripts/research/icebreaker_preflight.py --min-vol 1e6 --max-vol 5e7 --limit 25
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.core.exchange.ccxt_adapter import CCXTAdapter
from src.core.models.base import Exchange
from src.icebreaker.universe import fetch_perp_universe, select_overlap_universe

EXECUTION = Exchange.BYBIT
DETECT = [Exchange.MEXC, Exchange.KUCOIN]
WATCHLIST_PATH = ROOT / "config" / "icebreaker_watchlist.json"


async def _public_swap_adapter(exchange: Exchange) -> CCXTAdapter:
    options = {"defaultType": "swap"}
    # Bybit load_markets otherwise fetches options per-baseCoin (slow + flaky).
    # We only ever want linear USDT perps, so restrict the categories loaded.
    if exchange == Exchange.BYBIT:
        options["fetchMarkets"] = ["linear"]
    adapter = CCXTAdapter(
        exchange=exchange,
        testnet=False,
        options=options,
    )
    await adapter.connect()
    return adapter


async def run(min_vol: float, max_vol: float, limit: int | None, write: bool) -> int:
    exchanges = [EXECUTION, *DETECT]
    perps: dict[str, dict] = {}
    adapters: list[CCXTAdapter] = []
    try:
        for ex in exchanges:
            try:
                adapter = await _public_swap_adapter(ex)
            except Exception as e:  # noqa: BLE001
                print(f"  [WARN] {ex.value}: connect failed — {e}")
                continue
            adapters.append(adapter)
            uni = await fetch_perp_universe(adapter)
            perps[ex.value] = uni
            print(f"  {ex.value:14s} USDT-perps: {len(uni)}")
    finally:
        for a in adapters:
            try:
                await a.disconnect()
            except Exception:
                pass

    if EXECUTION.value not in perps:
        print("\n[FAIL] execution venue (bybit) unavailable — cannot proceed.")
        return 1

    candidates = select_overlap_universe(
        perps,
        execution_exchange=EXECUTION.value,
        detect_exchanges=[d.value for d in DETECT],
        min_volume_usd=min_vol,
        max_volume_usd=max_vol,
        limit=limit,
    )

    print("\n" + "=" * 78)
    print(f"  OVERLAP: {len(candidates)} illiquid perps tradable on Bybit + a detect venue")
    print(f"  band: ${min_vol:,.0f} .. ${max_vol:,.0f} (Bybit 24h quote vol)")
    print("=" * 78)
    print(f"  {'base':<12}{'bybit_vol':>14}  detect venues")
    for c in candidates:
        venues = ", ".join(f"{ex}(${v/1e6:.1f}M)" for ex, v in c.detect_volume_usd.items())
        print(f"  {c.base:<12}{c.execution_volume_usd:>14,.0f}  {venues}")

    if not candidates:
        print("\n[STOP] No overlap in band — widen the band or reconsider the premise.")
        return 2

    if write:
        payload = {
            "_meta": {
                "execution_exchange": EXECUTION.value,
                "detect_exchanges": [d.value for d in DETECT],
                "min_volume_usd": min_vol,
                "max_volume_usd": max_vol,
            },
            "symbols": {
                c.base: {
                    "enabled": True,
                    "execution": {EXECUTION.value: c.execution_symbol},
                    "detect": c.detect_venues,
                }
                for c in candidates
            },
        }
        WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        WATCHLIST_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\n  seed watchlist -> {WATCHLIST_PATH.relative_to(ROOT)} ({len(candidates)} symbols)")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--min-vol", type=float, default=1_000_000.0)
    p.add_argument("--max-vol", type=float, default=50_000_000.0)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--no-write", action="store_true", help="don't write the watchlist")
    args = p.parse_args()
    rc = asyncio.run(run(args.min_vol, args.max_vol, args.limit, write=not args.no_write))
    sys.exit(rc)


if __name__ == "__main__":
    main()
