# Trading Infrastructure

A modular research-and-execution platform for systematic crypto trading, built around a
strict separation between **signal research**, **statistically honest validation**, and
**paper/live execution**. The emphasis is not on any single "winning" strategy but on the
*process*: turning a hypothesis into a backtest, subjecting it to leakage- and
overfitting-resistant validation, and only then wiring it into an event-driven engine.

> ⚠️ **Research / educational project.** Nothing here is financial advice. Strategies are
> studied for methodology; most research write-ups end in a *rejected* verdict — which is
> the point.

---

## Why this project is interesting

Most trading repos are a single notebook with an in-sample equity curve. This one is
organized like production data-science:

- **Validation-first.** A dedicated `src/validation/` package implements the checks that
  actually decide whether a signal is real:
  - **Combinatorial Purged Cross-Validation (CPCV)** with purging and embargo
    (`cpcv.py`) — the López de Prado approach to backtest cross-validation.
  - **Look-ahead / leakage detection** (`lookahead.py`).
  - **Statistical significance** tests and multiple-testing awareness (`statistical.py`).
  - **Regime-conditional** performance (`regime.py`) and a unified **scorecard**
    (`scorecard.py`) so every strategy is judged on the same rubric.
- **Realistic backtesting.** `src/backtest/` models trading **cost** and slippage
  (`cost.py`), computes risk-adjusted **metrics** (`metrics.py`), and runs from reusable
  **presets** (`presets.py`) rather than ad-hoc scripts.
- **Property-based tests.** Position sizing, decimal precision, and voting logic are
  verified with [Hypothesis](https://hypothesis.readthedocs.io/), not just fixed examples.
- **Reproducible research trail.** Each strategy family has a `docs/*_RESEARCH.md`
  write-up recording the hypothesis, method, results, and the accept/reject decision.

## Architecture

Event-driven core: an async engine consumes market data over WebSockets, strategies emit
signals onto an event bus, and a portfolio/execution layer turns approved signals into
(paper or live) orders.

```
src/
├── core/            # config (pydantic-settings), ccxt exchange adapter, WS feeds, redis bus
├── strategy/        # signal primitives: S/R levels, features, regime, signal construction
├── strategies/      # concrete strategies (base ABC + miro, volume_ranking, pairs_trading,
│                    #   funding_capture, v_bottom)
├── screener/        # universe selection / coins-in-play scanner
├── ai/              # chart generation, ML scorer, Claude-Vision chart scoring
├── backtest/        # cost model, metrics, runner, presets
├── validation/      # CPCV, look-ahead, statistical tests, regime, scorecard
├── engine/          # event bus, scheduler, daemon, state
├── paper_trading/   # trade tracker + P&L stats (SQLite)
├── portfolio/       # multi-strategy portfolio manager
├── execution/       # order execution layer
└── api/telegram/    # alerting bot
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the detailed design and
[`docs/RESEARCH.md`](docs/RESEARCH.md) for the index of research write-ups and decisions.

## Tech stack

| Area | Tools |
|---|---|
| Language | Python 3.11+ (asyncio throughout) |
| Exchange / data | `ccxt`, `websockets`, `aiohttp`, TimescaleDB (`asyncpg`), Redis |
| Data / ML | `pandas`, `numpy`, `pyarrow`, `scikit-learn`, `joblib` |
| API / bot | `FastAPI`, `uvicorn`, `python-telegram-bot` |
| AI | Anthropic Claude, OpenAI/OpenRouter (chart scoring) |
| Config / logging | `pydantic-settings`, `structlog`, `typer`, `rich` |
| Quality | `pytest` (+asyncio, cov, timeout), `hypothesis`, `freezegun`, `ruff`, `mypy --strict`, `pre-commit` |

## Testing & quality gates

- **46 test modules** — 37 unit, 6 integration, 3 property-based — under `tests/`.
- `mypy` runs in **strict** mode; `ruff` enforces a broad lint rule set.
- **CI** (`.github/workflows/ci.yml`) runs the suite with coverage on every push/PR.

```bash
pip install -e ".[dev]"
pytest                 # run the test suite
ruff check . && mypy src
```

## Quick start

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 2. Configure — copy the template and fill in your own keys
cp config/example.env .env      # exchange API keys, Telegram token, etc.

# 3. Explore research scripts (all read-only / backtests by default)
python scripts/research/pairs_trading_backtest.py
```

All credentials are read from `.env` via `pydantic-settings` — **no secrets live in the
codebase**.

## Repository layout

```
src/        application code (see Architecture above)
scripts/    entry points + research/ backtests
tests/      unit / integration / property
docs/       architecture, runbook, and per-strategy research write-ups
config/     example.env template
```

---

<sub>Author: osrivon-ui · License: MIT. Documentation and research notes are partly in
Russian.</sub>
