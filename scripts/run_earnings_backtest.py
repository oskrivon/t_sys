#!/usr/bin/env python3
"""Earnings PEAD backtest CLI.

Usage:
    # Phase 1: quantitative baseline (free, no LLM)
    python scripts/run_earnings_backtest.py --phase 1

    # Phase 1 with specific hold period
    python scripts/run_earnings_backtest.py --phase 1 --hold-days 5

    # Phase 1 with all hold periods comparison
    python scripts/run_earnings_backtest.py --phase 1 --all-holds

    # Phase 2: LLM-enhanced (requires FMP + OpenRouter keys)
    python scripts/run_earnings_backtest.py --phase 2

    # Fetch transcripts only (rate-limited, run over several days)
    python scripts/run_earnings_backtest.py --fetch-transcripts

    # Custom universe and dates
    python scripts/run_earnings_backtest.py --phase 1 --universe sp100 \
        --start 2023-01-01 --end 2025-12-31
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

from src.backtest.presets import us_equities_spot
from src.earnings.backtest import run_pead_backtest
from src.earnings.config import EarningsConfig
from src.earnings.db import get_events, get_llm_scores, init_db, upsert_events_batch
from src.earnings.fetcher import (
    fetch_earnings_calendar_fmp,
    fetch_earnings_calendar_yf,
    fetch_prices,
    fetch_transcript_fmp,
    get_universe,
    truncate_transcript,
)
from src.earnings.models import EarningsEvent
from src.earnings.report import (
    bucket_analysis,
    comparison_table,
    export_trades_csv,
    hold_period_analysis,
    print_report,
    save_equity_curve,
)

logger = structlog.get_logger()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Earnings PEAD Backtest")
    p.add_argument("--phase", type=int, default=1, choices=[1, 2],
                    help="Phase 1 = quantitative baseline, Phase 2 = LLM-enhanced")
    p.add_argument("--universe", default="sp100", choices=["sp100", "sp500"])
    p.add_argument("--start", default="2022-01-01", help="Backtest start date")
    p.add_argument("--end", default="2025-12-31", help="Backtest end date")
    p.add_argument("--hold-days", type=int, default=5, help="Hold period in trading days")
    p.add_argument("--all-holds", action="store_true",
                    help="Run all hold periods [1, 3, 5, 10] and compare")
    p.add_argument("--surprise-threshold", type=float, default=5.0,
                    help="EPS surprise %% threshold to trade")
    p.add_argument("--position-size", type=float, default=10_000,
                    help="Position size in USD")
    p.add_argument("--fetch-transcripts", action="store_true",
                    help="Only fetch and cache transcripts (for Phase 2 prep)")
    p.add_argument("--data-source", default="yfinance", choices=["yfinance", "fmp"],
                    help="Earnings calendar source")
    p.add_argument("--export-csv", action="store_true", help="Export trades to CSV")
    p.add_argument("--save-plot", action="store_true", help="Save equity curve PNG")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    config = EarningsConfig(
        universe=args.universe,
        start_date=args.start,
        end_date=args.end,
        hold_periods=[1, 3, 5, 10] if args.all_holds else [args.hold_days],
        position_size_usd=args.position_size,
        surprise_threshold_pct=args.surprise_threshold,
        fmp_api_key=os.getenv("FMP_API_KEY", ""),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
    )

    symbols = get_universe(config.universe)
    conn = init_db(config.db_path)

    print(f"=== Earnings PEAD Backtest ===")
    print(f"Universe: {config.universe} ({len(symbols)} symbols)")
    print(f"Period: {config.start_date} - {config.end_date}")
    print(f"Phase: {args.phase}")
    print()

    # ── Step 1: Fetch earnings calendar ──────────────────────────────
    print("Fetching earnings calendar...")
    if args.data_source == "fmp" and config.fmp_api_key:
        events = fetch_earnings_calendar_fmp(
            symbols, config.start_date, config.end_date,
            api_key=config.fmp_api_key,
            cache_dir=f"{config.cache_dir}/calendar",
        )
    else:
        events = fetch_earnings_calendar_yf(
            symbols, config.start_date, config.end_date,
            cache_dir=f"{config.cache_dir}/calendar",
        )

    # Filter events with actual EPS data
    events = [e for e in events if e.eps_actual is not None and e.eps_surprise_pct is not None]
    print(f"Events with EPS data: {len(events)}")

    # Save to DB
    upsert_events_batch(conn, events)

    # ── Fetch transcripts only mode ──────────────────────────────────
    if args.fetch_transcripts:
        if not config.fmp_api_key:
            print("ERROR: Set FMP_API_KEY env var for transcript fetching")
            sys.exit(1)

        print("\nFetching transcripts...")
        fetched = 0
        for event in events:
            q = (event.earnings_date.month - 1) // 3 + 1
            y = event.earnings_date.year
            result = fetch_transcript_fmp(
                event.symbol, y, q,
                api_key=config.fmp_api_key,
                cache_dir=f"{config.cache_dir}/transcripts",
            )
            if result:
                fetched += 1
            if fetched % 50 == 0 and fetched > 0:
                print(f"  Fetched {fetched} transcripts...")

        print(f"Done: {fetched} transcripts cached")
        return

    # ── Step 2: Fetch price data ─────────────────────────────────────
    print("Fetching price data...")
    unique_symbols = list({e.symbol for e in events})
    prices = fetch_prices(
        unique_symbols, config.start_date, config.end_date,
        cache_dir=f"{config.cache_dir}/prices",
    )
    print(f"Price data for {len(prices)} symbols")

    # ── Step 3: Run backtest ─────────────────────────────────────────
    cost_model = us_equities_spot()

    if args.phase == 1:
        _run_phase1(config, events, prices, cost_model, args)
    else:
        _run_phase2(config, events, prices, cost_model, conn, args)

    conn.close()


def _run_phase1(config, events, prices, cost_model, args) -> None:
    """Phase 1: Quantitative PEAD baseline."""
    print("\n--- Phase 1: Quantitative PEAD Baseline ---\n")

    if args.all_holds:
        # Compare all hold periods
        all_results: dict[int, list] = {}
        for hd in config.hold_periods:
            trades = run_pead_backtest(
                events, prices,
                hold_days=hd,
                surprise_threshold_pct=config.surprise_threshold_pct,
                position_size=config.position_size_usd,
            )
            cost_model.apply_all(trades)
            all_results[hd] = trades
            print_report(f"PEAD Baseline ({hd}d hold)", trades,
                        position_size=config.position_size_usd)

        # Hold period comparison
        hp_df = hold_period_analysis(all_results)
        if not hp_df.empty:
            print("\n--- Hold Period Comparison ---")
            print(hp_df.to_string(index=False))
            print()

        # Use best hold period for detailed analysis
        best_hd = max(all_results, key=lambda h: len(all_results[h]))
        trades = all_results[best_hd]
    else:
        trades = run_pead_backtest(
            events, prices,
            hold_days=args.hold_days,
            surprise_threshold_pct=config.surprise_threshold_pct,
            position_size=config.position_size_usd,
        )
        cost_model.apply_all(trades)

    # Bucket analysis
    bucket_df = bucket_analysis(trades, n_buckets=5)
    print_report(
        f"PEAD Baseline ({args.hold_days}d)" if not args.all_holds
        else f"PEAD Baseline (best: {best_hd}d)",
        trades,
        bucket_df=bucket_df,
        position_size=config.position_size_usd,
    )

    if args.export_csv:
        export_trades_csv(trades, "data/reports/earnings_pead_trades.csv")

    if args.save_plot:
        save_equity_curve(trades, "data/reports/earnings_pead_equity.png")


def _run_phase2(config, events, prices, cost_model, conn, args) -> None:
    """Phase 2: LLM-enhanced backtest."""
    print("\n--- Phase 2: LLM-Enhanced ---\n")

    if not config.openrouter_api_key:
        print("ERROR: Set OPENROUTER_API_KEY env var for Phase 2")
        sys.exit(1)

    # Score transcripts
    llm_scores = _score_transcripts(config, events, conn)
    if not llm_scores:
        print("No LLM scores available. Run --fetch-transcripts first.")
        sys.exit(1)

    print(f"LLM scores available: {len(llm_scores)}")

    # Run all strategy modes
    modes = ["surprise_only", "llm_only", "combined", "llm_filtered"]
    hd = args.hold_days
    results: dict[str, list] = {}

    for mode in modes:
        trades = run_pead_backtest(
            events, prices,
            hold_days=hd,
            surprise_threshold_pct=config.surprise_threshold_pct,
            position_size=config.position_size_usd,
            mode=mode,
            llm_scores=llm_scores,
        )
        cost_model.apply_all(trades)
        results[f"{mode}_{hd}d"] = trades
        print_report(f"{mode} ({hd}d hold)", trades,
                    position_size=config.position_size_usd)

    # Comparison table
    comp_df = comparison_table(results, position_size=config.position_size_usd)
    print("\n--- Strategy Comparison ---")
    print(comp_df.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print()

    # LLM value-add
    baseline_key = f"surprise_only_{hd}d"
    combined_key = f"combined_{hd}d"
    if baseline_key in results and combined_key in results:
        from src.earnings.report import compute_event_metrics
        base_m = compute_event_metrics(results[baseline_key])
        comb_m = compute_event_metrics(results[combined_key])
        if base_m and comb_m:
            print("--- LLM Value-Add ---")
            print(f"  Sharpe:    {base_m.sharpe_annualized:.2f} -> {comb_m.sharpe_annualized:.2f} "
                  f"(d={comb_m.sharpe_annualized - base_m.sharpe_annualized:+.2f})")
            print(f"  Win Rate:  {base_m.win_rate:.1%} -> {comb_m.win_rate:.1%} "
                  f"(d={comb_m.win_rate - base_m.win_rate:+.1%})")
        print()


def _score_transcripts(config, events, conn) -> dict[str, float]:
    """Score all available transcripts with LLM. Returns {key: composite_score}."""
    from src.earnings.db import get_llm_scores, save_llm_score
    from src.earnings.fetcher import truncate_transcript
    from src.earnings.scorer import TranscriptScorer

    # Check existing scores
    existing = get_llm_scores(conn, model=config.llm_model)
    llm_scores: dict[str, float] = {}
    for row in existing:
        key = f"{row['symbol']}_{row['earnings_date']}"
        llm_scores[key] = row["composite_score"]

    # Find events needing scoring
    to_score = []
    for event in events:
        key = f"{event.symbol}_{event.earnings_date.isoformat()}"
        if key in llm_scores:
            continue
        # Check if transcript is cached
        q = (event.earnings_date.month - 1) // 3 + 1
        y = event.earnings_date.year
        transcript_path = (
            Path(config.cache_dir) / "transcripts"
            / f"{event.symbol}_{y}_Q{q}.txt"
        )
        if transcript_path.exists() and transcript_path.stat().st_size > 100:
            to_score.append((event, transcript_path))

    if not to_score:
        return llm_scores

    print(f"Scoring {len(to_score)} transcripts with LLM...")
    scorer = TranscriptScorer(
        api_key=config.openrouter_api_key,
        model=config.llm_model,
        temperature=config.scorer_temperature,
    )

    async def _score_all():
        scored = 0
        for event, path in to_score:
            transcript = path.read_text(encoding="utf-8")
            transcript = truncate_transcript(transcript, max_words=5000)
            q = (event.earnings_date.month - 1) // 3 + 1

            result = await scorer.score(
                event, transcript,
                quarter_label=f"Q{q} {event.earnings_date.year}",
            )
            if result:
                score, prompt_hash, raw = result
                key = f"{event.symbol}_{event.earnings_date.isoformat()}"
                llm_scores[key] = score.composite_score

                save_llm_score(
                    conn, event.symbol, event.earnings_date.isoformat(),
                    config.llm_model, prompt_hash, raw, score,
                )
                scored += 1

                if scored % 20 == 0:
                    print(f"  Scored {scored}/{len(to_score)}...")

        print(f"  Done: {scored} new scores")

    asyncio.run(_score_all())
    return llm_scores


if __name__ == "__main__":
    main()
