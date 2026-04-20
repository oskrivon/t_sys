"""MiroScreener — main screener orchestration.

Scans coins for breakout/retest/zakol patterns, scores with ML,
optionally validates with Claude Vision, sends Telegram alerts.
Supports D1 level confirmation for higher-quality signals.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import structlog

from src.strategy.levels import get_rolling_levels
from src.strategy.signals import (
    detect_breakouts, detect_retests, detect_zakol,
    find_best_signal, build_signal,
)
from src.strategy.features import compute_features
from src.strategy.models import Breakout, Level, Signal, ScreenerResult

from .config import ScreenerConfig
from .state import (
    ScreenerState, BreakoutState,
    load_state, save_state,
)
from .data_fetcher import fetch_all_candles
from .coins_in_play import CoinsInPlayDetector

log = structlog.get_logger()

# Optional imports — graceful fallback
try:
    from src.paper_trading.tracker import PaperTrader
except ImportError:
    PaperTrader = None

try:
    from src.core.redis_bus import RedisBus, CH_SIGNALS_SCREENER
    from src.core.models.signals import ScreenerSignal, SignalType, SignalDirection
except ImportError:
    RedisBus = None


def _level_near_d1(level: Level, d1_levels: list[Level], proximity_pct: float) -> bool:
    """Check if a lower-TF level is near any D1 level."""
    for d1 in d1_levels:
        if abs(level.price - d1.price) / level.price * 100 < proximity_pct:
            return True
    return False


class MiroScreener:
    """Scans multiple coins for Miro strategy signals."""

    def __init__(
        self,
        config: ScreenerConfig,
        exchange,
        ml_scorer=None,
        vision_scorer=None,
        notifier=None,
        redis_bus=None,
    ):
        self.config = config
        self.exchange = exchange
        self.ml_scorer = ml_scorer
        self.vision_scorer = vision_scorer
        self.notifier = notifier
        self.redis_bus = redis_bus

        self.coins_detector = CoinsInPlayDetector(
            base_symbols=config.base_symbols,
            volume_ratio_threshold=config.volume_ratio_threshold,
            max_coins=config.max_coins,
        )

        self.state = load_state(config.state_file)
        # Paper trader: only if Redis not available (local dev fallback)
        self.paper_trader = PaperTrader() if PaperTrader and not redis_bus else None

    async def run_once(self) -> ScreenerResult:
        """Run a single scan across all coins."""
        now = datetime.now(timezone.utc)
        log.info("scan_start", timestamp=now.isoformat(), d1_mode=self.config.d1_mode)

        # 1. Get coins to scan
        if self.config.use_coins_in_play:
            symbols = await self.coins_detector.get_active_coins(self.exchange)
        else:
            symbols = self.config.base_symbols

        # 2. Fetch entry TF candle data
        datasets = await fetch_all_candles(
            self.exchange, symbols,
            timeframe=self.config.timeframe,
            limit=self.config.fetch_candle_limit,
            concurrency=self.config.fetch_concurrency,
        )

        # Filter out stale data (delisted coins with frozen candles)
        stale_cutoff = now.timestamp() - 2 * 86400  # older than 2 days
        stale = [sym for sym, df in datasets.items()
                 if len(df) > 0 and df.index[-1].timestamp() < stale_cutoff]
        for sym in stale:
            del datasets[sym]
        if stale:
            log.warning("stale_data_filtered", symbols=stale)

        # 3. Fetch D1 data if needed
        d1_datasets: dict[str, pd.DataFrame] = {}
        if self.config.d1_mode != "none":
            d1_datasets = await fetch_all_candles(
                self.exchange, list(datasets.keys()),
                timeframe="1d",
                limit=self.config.d1_candle_limit,
                concurrency=self.config.fetch_concurrency,
            )
            log.info("d1_data_fetched", symbols=len(d1_datasets))

        # 4. Scan each symbol for signals
        all_signals: list[Signal] = []
        for symbol, df in datasets.items():
            d1_df = d1_datasets.get(symbol)
            signals = self._scan_symbol(symbol, df, d1_df)
            all_signals.extend(signals)

        # 5. Score and alert
        alerted = 0
        for signal in all_signals:
            ts_key = signal.timestamp.isoformat() if signal.timestamp else now.isoformat()

            if self.state.was_alerted(signal.symbol, signal.signal_type.value, ts_key):
                continue

            # ML scoring
            if self.ml_scorer and self.ml_scorer.is_loaded:
                features = compute_features(
                    datasets[signal.symbol],
                    len(datasets[signal.symbol]) - 1,
                    signal.level,
                    signal.signal_type,
                    self.config.trend_sma,
                )
                signal.ml_score = self.ml_scorer.predict(features)

                if signal.ml_score is not None and signal.ml_score < self.config.ml_threshold:
                    log.debug("signal_below_ml_threshold",
                              symbol=signal.symbol, ml_score=signal.ml_score)
                    continue

            # Vision scoring (optional)
            if (
                self.config.vision_enabled
                and self.vision_scorer
                and signal.ml_score is not None
                and signal.ml_score >= self.config.vision_min_ml_score
            ):
                signal.vision_score = await self._get_vision_score(signal, datasets[signal.symbol])

                # Vision filter: skip if below min score
                if signal.vision_score is not None and signal.vision_score < self.config.vision_min_score:
                    log.info("signal_below_vision_threshold",
                             symbol=signal.symbol, vision_score=signal.vision_score,
                             min_score=self.config.vision_min_score)
                    continue

            # Send alert
            if self.notifier:
                try:
                    await self.notifier.send_signal_alert(signal, datasets.get(signal.symbol))
                    alerted += 1
                except Exception as e:
                    log.error("alert_send_error", symbol=signal.symbol, error=str(e))
            else:
                self._print_signal(signal)
                alerted += 1

            # Publish signal to Redis (for paper trading service + engine)
            if self.redis_bus and RedisBus:
                try:
                    redis_signal = ScreenerSignal(
                        signal_type=SignalType(signal.signal_type.value),
                        symbol=signal.symbol,
                        direction=SignalDirection("long" if signal.is_long else "short"),
                        timeframe=self.config.timeframe,
                        entry_price=signal.entry_price,
                        sl=signal.sl,
                        tp=signal.tp,
                        rr_ratio=signal.rr_ratio,
                        level_price=signal.level.price,
                        level_touches=signal.level.touches,
                        level_score=signal.level.score,
                        ml_score=signal.ml_score or 0.0,
                        vision_score=signal.vision_score,
                        volume_ratio=signal.volume_ratio or 0.0,
                        source=f"screener-{self.config.timeframe}",
                    )
                    await self.redis_bus.publish(
                        CH_SIGNALS_SCREENER,
                        redis_signal.to_redis(),
                        source=f"screener-{self.config.timeframe}",
                    )
                except Exception as e:
                    log.error("redis_publish_error", symbol=signal.symbol, error=str(e))

            # Fallback: record paper trade directly (local dev without Redis)
            if self.paper_trader:
                try:
                    self.paper_trader.record_signal(signal)
                except Exception as e:
                    log.error("paper_trade_record_error", symbol=signal.symbol, error=str(e))

            self.state.add_alert_key(signal.symbol, signal.signal_type.value, ts_key)

        # 6. Check open paper trades (only if using local paper trader)
        if self.paper_trader:
            try:
                resolved = await self.paper_trader.check_open_trades(self.exchange)
                if resolved:
                    log.info("paper_trades_resolved", count=len(resolved),
                             trades=[f"{r['symbol']} {r['status']} {r['pnl_pct']:+.2f}%" for r in resolved])
            except Exception as e:
                log.error("paper_trade_check_error", error=str(e))

        # 7. Save state
        self.state.last_run_ts = now.isoformat()
        self.state.cleanup()
        save_state(self.state, self.config.state_file)

        result = ScreenerResult(
            timestamp=now,
            symbols_scanned=len(datasets),
            signals_found=len(all_signals),
            signals_alerted=alerted,
            signals=all_signals,
        )

        log.info("scan_complete",
                  symbols=result.symbols_scanned,
                  signals=result.signals_found,
                  alerted=result.signals_alerted)
        return result

    def _get_d1_levels(self, d1_df: Optional[pd.DataFrame]) -> list[Level]:
        """Get D1 levels from daily data."""
        if d1_df is None or len(d1_df) < 50:
            return []
        cfg = self.config
        return get_rolling_levels(
            d1_df, len(d1_df) - 1,
            lookback=cfg.d1_level_lookback,
            min_touches=2,
            tolerance_pct=cfg.d1_level_tolerance_pct,
            min_level_age=cfg.d1_min_level_age,
            swing_order=3,
        )

    def _scan_symbol(
        self, symbol: str, df: pd.DataFrame, d1_df: Optional[pd.DataFrame] = None,
    ) -> list[Signal]:
        """Scan a single symbol for signals on the latest candle."""
        cfg = self.config

        if len(df) < cfg.level_lookback + 10:
            return []

        current_idx = len(df) - 1

        # Compute trend
        sma = df["close"].rolling(cfg.trend_sma).mean()
        trend = pd.Series(0, index=df.index)
        trend[df["close"] > sma] = 1
        trend[df["close"] < sma] = -1

        # Get D1 levels
        d1_levels = self._get_d1_levels(d1_df) if cfg.d1_mode != "none" else []

        # Get entry TF levels based on D1 mode
        if cfg.d1_mode == "d1_only":
            levels = d1_levels
        elif cfg.d1_mode == "d1_filter":
            raw_levels = get_rolling_levels(
                df, current_idx,
                lookback=cfg.level_lookback,
                min_touches=cfg.level_min_touches,
                tolerance_pct=cfg.level_tolerance_pct,
                min_level_age=cfg.min_level_age,
            )
            levels = [lv for lv in raw_levels
                      if _level_near_d1(lv, d1_levels, cfg.d1_proximity_pct)]
        else:  # "none"
            levels = get_rolling_levels(
                df, current_idx,
                lookback=cfg.level_lookback,
                min_touches=cfg.level_min_touches,
                tolerance_pct=cfg.level_tolerance_pct,
                min_level_age=cfg.min_level_age,
            )

        if not levels:
            return []

        # Detect breakouts and update state
        new_breakouts = detect_breakouts(df, current_idx, levels)
        saved_breakouts = self._get_saved_breakouts(symbol, current_idx)
        saved_breakouts.extend(new_breakouts)
        saved_breakouts = [b for b in saved_breakouts if current_idx - b.idx <= cfg.retest_window]
        self._save_breakouts(symbol, saved_breakouts, current_idx)

        # Find best signal
        best = find_best_signal(
            df, current_idx, levels, saved_breakouts,
            retest_window=cfg.retest_window,
            trend=trend,
        )

        if not best:
            return []

        sig_type, level, score = best
        close = df["close"].iloc[current_idx]
        ts = df["ts"].iloc[current_idx] if "ts" in df.columns else None

        signal = build_signal(symbol, sig_type, level, close, cfg.rr_ratio, timestamp=ts)
        if signal is None:
            return []

        # Volume ratio
        if current_idx >= 30:
            avg_vol = df["volume"].iloc[current_idx - 30 : current_idx].mean()
            if avg_vol > 0:
                signal.volume_ratio = float(df["volume"].iloc[current_idx] / avg_vol)

        return [signal]

    def _get_saved_breakouts(self, symbol: str, current_idx: int) -> list[Breakout]:
        states = self.state.recent_breakouts.get(symbol, [])
        return [s.to_breakout() for s in states]

    def _save_breakouts(self, symbol: str, breakouts: list[Breakout], current_idx: int) -> None:
        self.state.recent_breakouts[symbol] = [
            BreakoutState.from_breakout(b) for b in breakouts
        ]

    async def _get_vision_score(self, signal: Signal, df: pd.DataFrame) -> Optional[int]:
        from src.ai.chart_generator import generate_chart
        entry_idx = len(df) - 1
        chart_png = await asyncio.to_thread(
            generate_chart, df, entry_idx, [signal.level.price],
            signal.signal_type.value, signal.is_long,
            candles_before=60, candles_after=0,
        )
        if not chart_png:
            return None
        result = await self.vision_scorer.score(
            chart_png, signal.symbol,
            signal.signal_type.value, signal.is_long,
        )
        return result["score"] if result and "score" in result else None

    def _print_signal(self, signal: Signal) -> None:
        from src.api.telegram.formatters import format_signal_message
        print(f"\n{format_signal_message(signal)}")

    async def run_forever(self) -> None:
        """Run screener in a loop, aligned to candle closes."""
        from datetime import timedelta

        tf = self.config.timeframe
        if tf.endswith("h"):
            interval_hours = int(tf[:-1])
        elif tf.endswith("m"):
            interval_hours = int(tf[:-1]) / 60
        else:
            interval_hours = 4

        log.info("screener_starting", timeframe=tf, interval_hours=interval_hours,
                  d1_mode=self.config.d1_mode)

        while True:
            now = datetime.now(timezone.utc)

            if interval_hours >= 1:
                ih = int(interval_hours)
                next_close_hour = ((now.hour // ih) + 1) * ih
                next_close = now.replace(minute=0, second=0, microsecond=0)
                if next_close_hour >= 24:
                    next_close = next_close.replace(hour=0) + timedelta(days=1)
                else:
                    next_close = next_close.replace(hour=next_close_hour)
            else:
                interval_min = int(interval_hours * 60)
                next_min = ((now.minute // interval_min) + 1) * interval_min
                next_close = now.replace(second=0, microsecond=0)
                if next_min >= 60:
                    next_close = next_close.replace(minute=0) + timedelta(hours=1)
                else:
                    next_close = next_close.replace(minute=next_min)

            if next_close <= now:
                next_close += timedelta(hours=interval_hours)

            wait_seconds = (next_close - now).total_seconds() + self.config.candle_close_delay_sec

            log.info("waiting_for_candle_close",
                      next_close=next_close.isoformat(),
                      wait_seconds=int(wait_seconds))

            await asyncio.sleep(wait_seconds)

            try:
                result = await self.run_once()
                log.info("scan_cycle_done",
                          signals=result.signals_found,
                          alerted=result.signals_alerted)
            except Exception as e:
                log.error("scan_cycle_error", error=str(e))
                await asyncio.sleep(60)
