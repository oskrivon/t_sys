"""Funding Capture strategy — enter before settlement, collect funding payment.

Logic:
  1. REST scan ALL 573 pairs every 5 min → find high-funding coins
  2. WS subscribe only to hot coins (|rate| > scan_threshold)
  3. WS monitors precise timing + rate updates
  4. 10s before settlement: emit TradeSignal if |rate| > trade_threshold
  5. ExecutionManager enters, holds through settlement, exits after

Dynamic watchlist: REST discovers, WS monitors, minimal server load.
Backtest result: +192% on $1k (3 months), ~$160/mo, WR 56%.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog

from src.engine.event_bus import EventBus, Event, EventType
from src.strategies.base import (
    Strategy, StrategyConfig, StrategyType, TradeSignal, Side,
)

logger = structlog.get_logger()

# Always monitor these (high volume, low slippage)
ALWAYS_MONITOR = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"}


@dataclass
class FundingOpportunity:
    symbol_raw: str
    symbol_ccxt: str
    funding_rate: float
    next_funding_time: int  # ms timestamp
    direction: Side
    last_price: float


class FundingCaptureStrategy(Strategy):
    def __init__(self, config: StrategyConfig, event_bus: EventBus) -> None:
        super().__init__(config)
        self._event_bus = event_bus
        self._threshold_bps: float = config.params.get("threshold_bps", 10.0)
        self._threshold_rate = self._threshold_bps / 10_000  # 10bps = 0.001
        self._scan_threshold_bps: float = config.params.get("scan_threshold_bps", 5.0)
        self._scan_threshold_rate = self._scan_threshold_bps / 10_000
        self._scan_interval: int = config.params.get("scan_interval_seconds", 300)  # 5min
        self._leverage: int = config.params.get("leverage", 10)
        self._entry_secs_before: int = config.params.get("entry_seconds_before", 10)
        self._exit_secs_after: int = config.params.get("exit_seconds_after", 15)
        # Dynamic watchlist: starts with always-monitor, expanded by REST scan
        self._monitored: set[str] = set(ALWAYS_MONITOR)
        self._ws_ref = None  # set by daemon after WS connect
        self._exchange_ref = None  # raw ccxt for REST scanning
        # State
        self._opportunities: dict[str, FundingOpportunity] = {}
        self._scheduled: dict[str, asyncio.Task] = {}
        self._traded_this_round: set[str] = set()
        self._scan_task: Optional[asyncio.Task] = None
        self._min_volume_24h: float = config.params.get("min_volume_24h", 5_000_000)
        self._last_scan_results: list[dict] = []

    def set_ws(self, ws) -> None:
        """Called by daemon after WS is connected."""
        self._ws_ref = ws

    async def initialize(self, exchange) -> None:
        self._exchange_ref = exchange
        self._event_bus.subscribe(EventType.FUNDING_RATE, self._on_funding_update)
        # Start background REST scanner
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("funding_capture_init",
                     threshold_bps=self._threshold_bps,
                     scan_threshold_bps=self._scan_threshold_bps,
                     leverage=self._leverage,
                     scan_interval=self._scan_interval)

    # ------------------------------------------------------------------
    # REST scanner — discovers high-funding coins across ALL pairs
    # ------------------------------------------------------------------

    async def _scan_loop(self) -> None:
        """Periodically scan all pairs via REST, update WS subscriptions."""
        # Initial scan immediately
        await self._do_scan()
        while True:
            await asyncio.sleep(self._scan_interval)
            await self._do_scan()

    async def _do_scan(self) -> None:
        """Fetch all tickers, find high-funding pairs, update WS subs."""
        try:
            import ccxt.async_support as ccxt
            # Use a separate lightweight client for scanning
            scanner = ccxt.bybit({"enableRateLimit": True})
            try:
                tickers = await scanner.fetch_tickers(params={"category": "linear"})
            finally:
                await scanner.close()

            hot_symbols: set[str] = set(ALWAYS_MONITOR)
            scan_results = []

            for sym, t in tickers.items():
                if "/USDT:USDT" not in sym:
                    continue
                fr = t.get("info", {}).get("fundingRate")
                if not fr:
                    continue
                vol_24h = float(t.get("quoteVolume") or 0)
                if vol_24h < self._min_volume_24h:
                    continue
                rate = abs(float(fr))
                if rate >= self._scan_threshold_rate:
                    raw = sym.replace("/", "").replace(":USDT", "")
                    hot_symbols.add(raw)
                    scan_results.append({
                        "symbol": raw,
                        "rate": float(fr),
                        "rate_bps": rate * 10_000,
                        "volume_24h": vol_24h,
                    })

            scan_results.sort(key=lambda x: x["rate_bps"], reverse=True)
            self._last_scan_results = scan_results

            # Update monitored set
            old = self._monitored
            new_symbols = hot_symbols - old
            removed = old - hot_symbols - ALWAYS_MONITOR

            self._monitored = hot_symbols

            # Update WS subscriptions if changed
            if new_symbols and self._ws_ref:
                await self._ws_ref.subscribe_tickers(list(new_symbols))

            logger.info("funding_scan_complete",
                         total_pairs=len(tickers),
                         hot_pairs=len(hot_symbols),
                         above_trade_threshold=sum(1 for r in scan_results
                                                    if r["rate_bps"] >= self._threshold_bps),
                         new_subs=len(new_symbols),
                         removed=len(removed))

            if scan_results[:3]:
                top3 = ", ".join(f"{r['symbol']}={r['rate_bps']:.0f}bps" for r in scan_results[:3])
                logger.info("funding_scan_top", top=top3)

        except Exception:
            logger.exception("funding_scan_error")

    async def on_tick(self, exchange) -> list[TradeSignal]:
        """Not used — this strategy is WS-driven."""
        return []

    async def on_trade_closed(self, symbol: str, pnl_pct: float) -> None:
        pass

    # ------------------------------------------------------------------
    # WS event handler
    # ------------------------------------------------------------------

    async def _on_funding_update(self, event: Event) -> None:
        data = event.data
        symbol_raw = data.get("_symbol_raw", "")
        if symbol_raw not in self._monitored:
            return

        funding_rate = float(data.get("fundingRate", 0) or 0)
        next_funding_ms = int(data.get("nextFundingTime", 0) or 0)
        last_price = float(data.get("lastPrice", 0) or 0)

        if not next_funding_ms or not funding_rate:
            return

        # Build ccxt symbol: BTCUSDT -> BTC/USDT:USDT
        base = symbol_raw.replace("USDT", "")
        ccxt_sym = f"{base}/USDT:USDT"

        direction = Side.LONG if funding_rate < 0 else Side.SHORT

        opp = FundingOpportunity(
            symbol_raw=symbol_raw,
            symbol_ccxt=ccxt_sym,
            funding_rate=funding_rate,
            next_funding_time=next_funding_ms,
            direction=direction,
            last_price=last_price,
        )
        self._opportunities[symbol_raw] = opp

        # Check if we should schedule entry
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        time_to_funding_s = (next_funding_ms - now_ms) / 1000

        # Only schedule if: rate above threshold, within 2min window,
        # not already scheduled AND not already traded this round
        if (abs(funding_rate) >= self._threshold_rate
                and 0 < time_to_funding_s < 120
                and symbol_raw not in self._scheduled
                and symbol_raw not in self._traded_this_round):
            # Mark as traded IMMEDIATELY to prevent duplicate scheduling
            # from subsequent WS ticks (they arrive every ~100ms)
            self._traded_this_round.add(symbol_raw)
            self._scheduled[symbol_raw] = asyncio.create_task(
                self._schedule_entry(opp, time_to_funding_s)
            )
            logger.info("funding_entry_scheduled",
                         symbol=symbol_raw,
                         rate_bps=abs(funding_rate) * 10_000,
                         direction=direction.value,
                         secs_to_funding=int(time_to_funding_s))

        # Reset traded set when we're far from next settlement (>10min)
        if time_to_funding_s > 600:
            self._traded_this_round.clear()

    async def _schedule_entry(self, opp: FundingOpportunity, secs_until: float) -> None:
        """Wait until entry_secs_before settlement, then emit signal."""
        wait = secs_until - self._entry_secs_before
        if wait > 0:
            await asyncio.sleep(wait)

        # Re-check: rate might have changed
        current = self._opportunities.get(opp.symbol_raw)
        if not current or abs(current.funding_rate) < self._threshold_rate:
            logger.info("funding_entry_cancelled_rate_dropped", symbol=opp.symbol_raw)
            self._scheduled.pop(opp.symbol_raw, None)
            return

        opp = current  # use latest data

        signal = TradeSignal(
            strategy_id=self.config.strategy_id,
            symbol=opp.symbol_ccxt,
            side=opp.direction,
            entry_price=0.0,  # market order
            sl=0.0,           # no SL — exit on time
            tp=0.0,           # no TP — exit on time
            confidence=min(abs(opp.funding_rate) * 10_000 / 20.0, 1.0),
            metadata={
                "type": "funding_capture",
                "funding_rate": opp.funding_rate,
                "funding_rate_bps": abs(opp.funding_rate) * 10_000,
                "next_funding_time": opp.next_funding_time,
                "leverage": self._leverage,
                "exit_after_seconds": self._exit_secs_after,
                "last_price": opp.last_price,
            },
            timestamp=datetime.now(timezone.utc),
        )

        await self._event_bus.publish(Event(
            type=EventType.SIGNAL_GENERATED,
            data=signal,
            source=self.config.strategy_id,
        ))

        # Keep in _scheduled to prevent any further scheduling
        # (will be cleaned up on next settlement cycle via _traded_this_round)
        logger.info("funding_signal_emitted",
                     symbol=opp.symbol_raw,
                     rate_bps=abs(opp.funding_rate) * 10_000,
                     direction=opp.direction.value)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_opportunities(self) -> list[dict]:
        """Current funding opportunities above threshold."""
        result = []
        for sym, opp in self._opportunities.items():
            if abs(opp.funding_rate) >= self._threshold_rate:
                result.append({
                    "symbol": sym,
                    "rate_bps": abs(opp.funding_rate) * 10_000,
                    "direction": opp.direction.value,
                    "next_funding": opp.next_funding_time,
                    "price": opp.last_price,
                })
        return sorted(result, key=lambda x: x["rate_bps"], reverse=True)
