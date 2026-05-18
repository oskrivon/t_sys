"""Funding Capture strategy — enter before settlement, collect funding payment.

Logic:
  1. REST scan ALL pairs every 5 min → find high-funding coins
  2. WS subscribe only to hot coins (|rate| > scan_threshold)
  3. WS monitors precise timing + rate updates
  4. T-10s: pre-compute qty + set leverage (slow, ~500ms)
  5. T-2s: fire market order (fast, ~200ms)
  6. T-0: settlement — funding credited
  7. Exit on execType=Funding WS event (or timeout)

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
    funding_interval_h: int = 8  # 4 or 8 — used to normalize rate for threshold comparison


class FundingCaptureStrategy(Strategy):
    def __init__(self, config: StrategyConfig, event_bus: EventBus, state=None) -> None:
        super().__init__(config)
        self._event_bus = event_bus
        self._state = state  # StateManager — for logging skips to trades_log
        self._threshold_bps: float = config.params.get("threshold_bps", 10.0)
        self._threshold_rate = self._threshold_bps / 10_000  # 10bps = 0.001
        self._scan_threshold_bps: float = config.params.get("scan_threshold_bps", 5.0)
        self._scan_threshold_rate = self._scan_threshold_bps / 10_000
        self._scan_interval: int = config.params.get("scan_interval_seconds", 300)  # 5min
        self._leverage: int = config.params.get("leverage", 10)
        self._entry_secs_before: int = config.params.get("entry_seconds_before", 10)
        self._exit_secs_after: int = config.params.get("exit_seconds_after", 15)
        self._min_volume_24h: float = config.params.get("min_volume_24h", 5_000_000)
        self._target_notional: float = config.params.get("target_notional", 0.0)  # 0 = use full balance
        self._max_notional: float = config.params.get("max_notional", 500.0)  # safety cap
        self._balance_fraction: float = config.params.get("balance_fraction", 0.9)  # use 90% of free balance
        self._min_book_depth_mult: float = config.params.get("min_book_depth_mult", 2.0)
        self._max_spread_bps: float = config.params.get("max_spread_bps", 5.0)
        self._blacklist: set[str] = set(config.params.get("blacklist", []))
        # Dynamic watchlist: starts with always-monitor, expanded by REST scan
        self._monitored: set[str] = set(ALWAYS_MONITOR)
        self._ws_ref = None  # set by daemon after WS connect
        self._exchange_ref = None  # raw ccxt for REST scanning
        # State
        self._opportunities: dict[str, FundingOpportunity] = {}
        self._scheduled: dict[str, asyncio.Task] = {}
        self._traded_this_round: set[str] = set()
        self._scan_task: Optional[asyncio.Task] = None
        self._last_scan_results: list[dict] = []
        # Pre-computed for fast entry: symbol -> {qty, ...}
        self._precomputed: dict[str, dict] = {}
        # Funding interval per symbol: BTCUSDT -> 8, SIRENUSDT -> 4
        self._funding_intervals: dict[str, int] = {}

    def set_ws(self, ws) -> None:
        """Called by daemon after WS is connected."""
        self._ws_ref = ws

    def _log_skip(self, symbol: str, reason: str, details: dict | None = None) -> None:
        """Record skipped funding opportunity to trades_log for post-analysis."""
        if not self._state:
            return
        meta = {"reason": reason}
        if details:
            meta.update(details)
        self._state.log_trade(
            symbol=symbol,
            strategy_id=self.config.strategy_id,
            side="",
            action="skip",
            metadata=meta,
        )

    async def initialize(self, exchange) -> None:
        self._exchange_ref = exchange
        self._event_bus.subscribe(EventType.FUNDING_RATE, self._on_funding_update)
        # Start background REST scanner
        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info("funding_capture_init",
                     threshold_bps=self._threshold_bps,
                     scan_threshold_bps=self._scan_threshold_bps,
                     leverage=self._leverage,
                     scan_interval=self._scan_interval,
                     min_volume_24h=self._min_volume_24h,
                     target_notional=self._target_notional,
                     max_spread_bps=self._max_spread_bps)

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
            # Use a separate lightweight client for scanning (same exchange as engine)
            exchange_id = getattr(self._exchange_ref, "id", "bybit") if self._exchange_ref else "bybit"
            scanner_cls = getattr(ccxt, exchange_id)
            scanner_opts = {"enableRateLimit": True}
            if exchange_id == "binance":
                scanner_opts["options"] = {"defaultType": "swap"}
            scanner = scanner_cls(scanner_opts)
            try:
                # Fetch tickers for volume filter
                params = {"category": "linear"} if exchange_id == "bybit" else {}
                tickers = await scanner.fetch_tickers(params=params)

                # Fetch funding rates — Bybit embeds in tickers, Binance needs separate call
                funding_map: dict[str, tuple[float, int]] = {}  # sym -> (rate, next_funding_ms)
                if exchange_id == "bybit":
                    for sym, t in tickers.items():
                        fr = t.get("info", {}).get("fundingRate")
                        if fr:
                            nft = int(t.get("info", {}).get("nextFundingTime", 0) or 0)
                            funding_map[sym] = (float(fr), nft)
                else:
                    # Binance/others: use fetch_funding_rates()
                    rates = await scanner.fetch_funding_rates()
                    for sym, r in rates.items():
                        fr = r.get("fundingRate")
                        if fr is not None:
                            nft = int(r.get("fundingTimestamp") or r.get("info", {}).get("nextFundingTime", 0) or 0)
                            funding_map[sym] = (float(fr), nft)

                # Fetch funding intervals (Binance: API endpoint; Bybit: from markets)
                if exchange_id == "binance":
                    await self._fetch_funding_intervals(scanner)
                elif exchange_id == "bybit":
                    self._load_bybit_intervals(scanner)
            finally:
                await scanner.close()

            hot_symbols: set[str] = set(ALWAYS_MONITOR)
            scan_results = []

            for sym, t in tickers.items():
                if "/USDT:USDT" not in sym:
                    continue
                if sym not in funding_map:
                    continue
                fr_val, nft = funding_map[sym]
                if fr_val == 0:
                    continue
                vol_24h = float(t.get("quoteVolume") or 0)
                if vol_24h < self._min_volume_24h:
                    continue
                raw = sym.replace("/", "").replace(":USDT", "")
                base = raw.replace("USDT", "")
                if base in self._blacklist:
                    continue
                interval_h = self._funding_intervals.get(raw, 8)
                rate = abs(fr_val)
                # Normalize to 8h-equivalent for threshold comparison:
                # a 10bps rate on 4h schedule = 20bps effective (2x settlements/day)
                rate_8h_equiv = rate * (8 / interval_h)
                if rate_8h_equiv >= self._scan_threshold_rate:
                    hot_symbols.add(raw)
                    scan_results.append({
                        "symbol": raw,
                        "rate": fr_val,
                        "rate_bps": rate * 10_000,
                        "rate_8h_bps": rate_8h_equiv * 10_000,
                        "volume_24h": vol_24h,
                        "next_funding_ms": nft,
                        "interval_h": interval_h,
                    })

            scan_results.sort(key=lambda x: x["rate_8h_bps"], reverse=True)
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
                top3 = ", ".join(
                    f"{r['symbol']}={r['rate_bps']:.0f}bps" + (f"/{r['interval_h']}h" if r['interval_h'] != 8 else "")
                    for r in scan_results[:3]
                )
                logger.info("funding_scan_top", top=top3)

            # Log settlement schedule for coins above trade threshold
            self._log_settlement_schedule(scan_results)

        except Exception:
            logger.exception("funding_scan_error")

    async def _fetch_funding_intervals(self, scanner) -> None:
        """Fetch per-symbol funding interval from Binance's fundingInfo endpoint."""
        try:
            import aiohttp
            url = "https://fapi.binance.com/fapi/v1/fundingInfo"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    data = await resp.json()
            for entry in data:
                sym = entry.get("symbol", "")
                interval = int(entry.get("fundingIntervalHours", 8))
                self._funding_intervals[sym] = interval
            n_4h = sum(1 for v in self._funding_intervals.values() if v == 4)
            logger.info("funding_intervals_loaded",
                        total=len(self._funding_intervals), interval_4h=n_4h)
        except Exception:
            logger.warning("funding_intervals_fetch_failed")

    def _load_bybit_intervals(self, scanner) -> None:
        """Load funding intervals from Bybit markets (already loaded by ccxt)."""
        try:
            for sym, m in scanner.markets.items():
                if "/USDT:USDT" not in sym:
                    continue
                fi_min = m.get("info", {}).get("fundingInterval")
                if fi_min:
                    raw = sym.replace("/", "").replace(":USDT", "")
                    interval_h = int(fi_min) // 60
                    if interval_h > 0:
                        self._funding_intervals[raw] = interval_h
            n_4h = sum(1 for v in self._funding_intervals.values() if v == 4)
            logger.info("funding_intervals_loaded",
                        total=len(self._funding_intervals), interval_4h=n_4h)
        except Exception:
            logger.warning("funding_intervals_load_failed_bybit")

    def _log_settlement_schedule(self, scan_results: list[dict]) -> None:
        """Group tradeable coins by next settlement time for visibility."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # Group by settlement hour
        schedule: dict[str, list[str]] = {}
        for r in scan_results:
            if r["rate_bps"] < self._threshold_bps:
                continue
            nft = r.get("next_funding_ms", 0)
            if not nft:
                continue
            dt = datetime.fromtimestamp(nft / 1000, tz=timezone.utc)
            slot = dt.strftime("%H:%M")
            mins_until = int((nft - now_ms) / 60_000)
            entry = f"{r['symbol']}={r['rate_bps']:.0f}bps"
            schedule.setdefault(slot, []).append((mins_until, entry))

        if schedule:
            parts = []
            for slot in sorted(schedule.keys()):
                items = schedule[slot]
                mins = items[0][0]
                coins = "+".join(e for _, e in items[:3])
                parts.append(f"{slot}({mins}m):{coins}")
            logger.info("funding_settlement_schedule", slots="; ".join(parts))

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
        base = symbol_raw[:-4] if symbol_raw.endswith("USDT") else symbol_raw
        if base in self._blacklist:
            return

        funding_rate = float(data.get("fundingRate", 0) or 0)
        next_funding_ms = int(data.get("nextFundingTime", 0) or 0)
        last_price = float(data.get("lastPrice", 0) or 0)

        if not next_funding_ms or not funding_rate:
            return

        # Build ccxt symbol: BTCUSDT -> BTC/USDT:USDT
        # Use suffix strip, not replace() — replace removes ALL occurrences
        base = symbol_raw[:-4] if symbol_raw.endswith("USDT") else symbol_raw
        ccxt_sym = f"{base}/USDT:USDT"

        direction = Side.LONG if funding_rate < 0 else Side.SHORT
        interval_h = self._funding_intervals.get(symbol_raw, 8)

        # Binance @markPrice WS always reports next 8h slot in T field,
        # ignoring 4h/1h sub-intervals. For sub-8h coins, compute real
        # next settlement: ceil(now) to next interval_h boundary.
        if interval_h < 8:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            interval_ms = interval_h * 3600 * 1000
            # Next settlement = ceil(now / interval_ms) * interval_ms
            next_funding_ms = ((now_ms // interval_ms) + 1) * interval_ms

        opp = FundingOpportunity(
            symbol_raw=symbol_raw,
            symbol_ccxt=ccxt_sym,
            funding_rate=funding_rate,
            next_funding_time=next_funding_ms,
            direction=direction,
            last_price=last_price,
            funding_interval_h=interval_h,
        )
        self._opportunities[symbol_raw] = opp

        # Check if we should schedule entry
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        time_to_funding_s = (next_funding_ms - now_ms) / 1000

        # Use RAW per-settlement rate for threshold, not 8h-equivalent.
        # Costs (commission + slippage) are per-trade, so a 1h coin at
        # 5bps raw is unprofitable even though 8h-equiv = 40bps.
        raw_rate = abs(funding_rate)

        # Only schedule if: rate above threshold, within 2min window,
        # not already scheduled AND not already traded this round
        if (raw_rate >= self._threshold_rate
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

        # Reset traded set for THIS symbol when far from next settlement (>10min)
        if time_to_funding_s > 600:
            self._traded_this_round.discard(symbol_raw)

    @staticmethod
    def _is_weekend_window() -> bool:
        """Check if we're in weekend strategy window (Fri 21:00 - Sun 23:00 UTC).

        During this window, funding capture should not trade to avoid
        margin conflicts with the weekend BTC position.
        """
        now = datetime.now(timezone.utc)
        wd = now.weekday()  # 0=Mon ... 4=Fri, 5=Sat, 6=Sun
        h = now.hour
        # Friday 21:00+ or Saturday all day or Sunday before 23:00
        if wd == 4 and h >= 21:
            return True
        if wd == 5:
            return True
        if wd == 6 and h < 23:
            return True
        return False

    async def _schedule_entry(self, opp: FundingOpportunity, secs_until: float) -> None:
        """Wait, pre-compute, then fire order at T-2s.

        Timeline:
          T-entry_secs_before: pre-compute qty + set leverage (slow, ~500ms)
          T-2s: fire create_order (fast, ~200ms)
          T-0: settlement — we're already in position
        """
        # Weekend guard: don't trade during weekend strategy window
        if self._is_weekend_window():
            logger.info("funding_entry_skipped_weekend", symbol=opp.symbol_raw)
            self._scheduled.pop(opp.symbol_raw, None)
            return

        # Phase 1: wait until pre-compute window
        wait_precompute = secs_until - self._entry_secs_before
        if wait_precompute > 0:
            await asyncio.sleep(wait_precompute)

        # Re-check: rate might have changed
        current = self._opportunities.get(opp.symbol_raw)
        if current:
            effective_rate = abs(current.funding_rate) * (8 / current.funding_interval_h)
        else:
            effective_rate = 0
        if not current or effective_rate < self._threshold_rate:
            logger.info("funding_entry_cancelled_rate_dropped", symbol=opp.symbol_raw)
            current_bps = abs(current.funding_rate) * 10_000 if current else 0
            self._log_skip(opp.symbol_ccxt, "rate_dropped", {
                "funding_bps": round(current_bps, 1),
                "threshold_bps": self._threshold_bps,
                "interval_h": current.funding_interval_h if current else 8,
                "stage": "pre_precompute",
            })
            self._scheduled.pop(opp.symbol_raw, None)
            return

        opp = current  # use latest data

        # Phase 1.5: ensure private WS is alive for funding_credited event
        if self._ws_ref and hasattr(self._ws_ref, "ensure_private_alive"):
            try:
                await self._ws_ref.ensure_private_alive()
            except Exception:
                logger.warning("funding_ws_health_check_failed", symbol=opp.symbol_raw)

        # Phase 2: pre-compute (BEFORE hot path) — leverage + qty
        pre = await self._precompute_entry(opp)
        if not pre:
            # _precompute_entry logs specific reason internally
            self._scheduled.pop(opp.symbol_raw, None)
            return

        # Phase 3: wait until 5s before settlement, then fire order
        # (limit order needs ~3s to fill, market fallback at T-2s)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        remaining_s = (opp.next_funding_time - now_ms) / 1000
        fire_wait = remaining_s - 5.0
        if fire_wait > 0:
            await asyncio.sleep(fire_wait)

        # Final re-check
        current = self._opportunities.get(opp.symbol_raw)
        if current:
            effective_rate_final = abs(current.funding_rate) * (8 / current.funding_interval_h)
        else:
            effective_rate_final = 0
        if not current or effective_rate_final < self._threshold_rate:
            logger.info("funding_entry_cancelled_rate_dropped_final", symbol=opp.symbol_raw)
            current_bps = abs(current.funding_rate) * 10_000 if current else 0
            self._log_skip(opp.symbol_ccxt, "rate_dropped", {
                "funding_bps": round(current_bps, 1),
                "threshold_bps": self._threshold_bps,
                "interval_h": current.funding_interval_h if current else 8,
                "stage": "final_t2s",
            })
            self._scheduled.pop(opp.symbol_raw, None)
            return
        opp = current

        # Snapshot orderbook at T-2s for data collection
        book_snapshot = {}
        if self._exchange_ref:
            try:
                ob = await self._exchange_ref.fetch_order_book(opp.symbol_ccxt, limit=5)
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                book_snapshot = {
                    "bid1_usd": round(bids[0][1] * bids[0][0], 2) if bids else 0,
                    "ask1_usd": round(asks[0][1] * asks[0][0], 2) if asks else 0,
                    "bid5_usd": round(sum(b[1] * b[0] for b in bids[:5]), 2),
                    "ask5_usd": round(sum(a[1] * a[0] for a in asks[:5]), 2),
                    "spread_bps": round(
                        (asks[0][0] - bids[0][0]) / bids[0][0] * 10000, 1
                    ) if bids and asks else 0,
                }
            except Exception:
                logger.warning("funding_book_snapshot_failed", symbol=opp.symbol_raw)

        # Spread filter: reject wide-spread books (OOS-validated on 76 trades)
        spread = book_snapshot.get("spread_bps", 0)
        if spread >= self._max_spread_bps:
            logger.info("funding_spread_rejected",
                        symbol=opp.symbol_raw,
                        spread_bps=spread,
                        max_spread_bps=self._max_spread_bps)
            self._log_skip(opp.symbol_ccxt, "spread_too_wide", {
                "funding_bps": round(abs(opp.funding_rate) * 10_000, 1),
                "spread_bps": spread,
                "max_spread_bps": self._max_spread_bps,
                "book": book_snapshot,
            })
            self._scheduled.pop(opp.symbol_raw, None)
            return

        signal = TradeSignal(
            strategy_id=self.config.strategy_id,
            symbol=opp.symbol_ccxt,
            side=opp.direction,
            entry_price=0.0,
            sl=0.0,
            tp=0.0,
            confidence=min(abs(opp.funding_rate) * 10_000 / 20.0, 1.0),
            metadata={
                "type": "funding_capture",
                "funding_rate": opp.funding_rate,
                "funding_rate_bps": abs(opp.funding_rate) * 10_000,
                "next_funding_time": opp.next_funding_time,
                "leverage": self._leverage,
                "exit_after_seconds": self._exit_secs_after,
                "target_notional": pre.get("notional_target", self._target_notional),
                "last_price": opp.last_price,
                "_precomputed_qty": pre["qty"],
                "_leverage_set": True,
                "book_precompute": pre.get("book_precompute", {}),
                "book_t2s": book_snapshot,
                "funding_interval_h": opp.funding_interval_h,
            },
            timestamp=datetime.now(timezone.utc),
        )

        logger.info("funding_signal_emitted",
                     symbol=opp.symbol_raw,
                     rate_bps=abs(opp.funding_rate) * 10_000,
                     direction=opp.direction.value,
                     qty=pre["qty"])

        await self._event_bus.publish(Event(
            type=EventType.SIGNAL_GENERATED,
            data=signal,
            source=self.config.strategy_id,
        ))

    async def _precompute_entry(self, opp: FundingOpportunity) -> dict | None:
        """Pre-compute qty, set leverage, check book depth BEFORE the hot path."""
        try:
            exchange = self._exchange_ref
            if not exchange:
                return None

            # Set leverage (idempotent, cached by exchange after first call)
            try:
                await exchange.set_leverage(self._leverage, opp.symbol_ccxt)
            except Exception:
                pass  # "not modified" or already set

            # Determine notional: dynamic from balance or fixed
            if self._target_notional > 0:
                notional_target = self._target_notional
            else:
                # Fetch free balance and compute max notional
                try:
                    bal = await exchange.fetch_balance({"type": "swap"})
                    free_usdt = float(bal.get("USDT", {}).get("free", 0) or 0)
                    notional_target = free_usdt * self._balance_fraction
                    notional_target = min(notional_target, self._max_notional)
                    logger.info("funding_dynamic_notional",
                                symbol=opp.symbol_raw,
                                free_usdt=round(free_usdt, 2),
                                notional=round(notional_target, 2))
                except Exception:
                    logger.warning("funding_balance_fetch_failed", symbol=opp.symbol_raw)
                    notional_target = 25.0  # fallback

            # Compute qty from last_price
            try:
                market = exchange.market(opp.symbol_ccxt)
            except Exception:
                logger.warning("funding_symbol_not_found",
                               symbol=opp.symbol_raw,
                               ccxt_sym=opp.symbol_ccxt)
                self._log_skip(opp.symbol_ccxt, "symbol_not_found", {
                    "funding_bps": round(abs(opp.funding_rate) * 10_000, 1),
                })
                return None
            min_qty = float(market.get("limits", {}).get("amount", {}).get("min", 1))
            qty_step = float(market.get("precision", {}).get("amount", min_qty))

            if notional_target > 0 and opp.last_price > 0:
                raw_qty = notional_target / opp.last_price
                if qty_step > 0:
                    raw_qty = int(raw_qty / qty_step) * qty_step
                qty = max(raw_qty, min_qty)
            else:
                qty = min_qty

            notional = qty * opp.last_price

            # Snapshot orderbook at precompute time (T-10s) for data collection.
            # Log thin books but do NOT block — we need more data to validate
            # whether the filter reliably cuts losing trades.
            book_pre = {}
            try:
                ob = await exchange.fetch_order_book(opp.symbol_ccxt, limit=5)
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                bid1_usd = bids[0][1] * bids[0][0] if bids else 0
                ask1_usd = asks[0][1] * asks[0][0] if asks else 0
                book_pre = {
                    "bid1_usd": round(bid1_usd, 2),
                    "ask1_usd": round(ask1_usd, 2),
                    "bid5_usd": round(sum(b[1] * b[0] for b in bids[:5]), 2),
                    "ask5_usd": round(sum(a[1] * a[0] for a in asks[:5]), 2),
                    "spread_bps": round(
                        (asks[0][0] - bids[0][0]) / bids[0][0] * 10000, 1
                    ) if bids and asks else 0,
                }
                min_depth = min(bid1_usd, ask1_usd)
                required = notional * self._min_book_depth_mult
                if min_depth < required:
                    logger.warning("funding_thin_book",
                                   symbol=opp.symbol_raw,
                                   bid1_usd=round(bid1_usd, 1),
                                   ask1_usd=round(ask1_usd, 1),
                                   notional=round(notional, 2),
                                   required=round(required, 2))
                    self._log_skip(opp.symbol_ccxt, "thin_book", {
                        "funding_bps": round(abs(opp.funding_rate) * 10_000, 1),
                        "bid1_usd": round(bid1_usd, 1),
                        "ask1_usd": round(ask1_usd, 1),
                        "notional": round(notional, 2),
                        "required": round(required, 2),
                        "min_book_depth_mult": self._min_book_depth_mult,
                    })
                    return None
            except Exception:
                logger.warning("funding_book_check_failed", symbol=opp.symbol_raw)

            logger.info("funding_precomputed",
                        symbol=opp.symbol_raw,
                        qty=qty,
                        price=opp.last_price,
                        notional=round(notional, 2))
            return {"qty": qty, "book_precompute": book_pre, "notional_target": notional_target}

        except Exception:
            logger.exception("funding_precompute_failed", symbol=opp.symbol_raw)
            self._log_skip(opp.symbol_ccxt, "precompute_failed", {
                "funding_bps": round(abs(opp.funding_rate) * 10_000, 1),
            })
            return None

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
