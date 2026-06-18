"""Icebreaker universe selection — cross-exchange symbol overlap (pre-flight).

The wall-eating strategy detects a wall on *any* venue but always enters on the
execution venue (Bybit). So the tradable universe is the set of perps that are:

  1. Listed as a USDT perpetual on the execution venue (Bybit) — otherwise we
     cannot enter the trade at all.
  2. Listed as a USDT perpetual on at least one *detect* venue (MEXC/KuCoin/...)
     — otherwise there is no second book to watch for the wall.
  3. Within an illiquid-but-tradable liquidity band on the execution venue —
     too liquid = a major (excluded by design); too illiquid = un-fillable.
  4. Not in the big-cap exclusion list used by the original backtest.

All selection logic lives in pure functions over plain dataclasses so it can be
tested without any network access. The thin network layer (`fetch_perp_universe`)
only turns exchange adapters into the `PerpInfo` inputs these functions consume.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

# Big-cap bases excluded by the original icebreaker backtest (описание + backtest.xlsx).
DEFAULT_EXCLUDE_BASES: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "LINK", "LTC", "DOGE",
    "1000PEPE", "PEPE", "HYPE", "TRUMP",
})

# Quote assets we treat as "USDT-equivalent" stable perps.
STABLE_QUOTES: frozenset[str] = frozenset({"USDT"})

# Stable/pegged bases that look like alts but aren't tradable moves.
STABLE_BASES: frozenset[str] = frozenset({
    "USDC", "USDT", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD", "BUSD",
})


@dataclass(frozen=True)
class PerpInfo:
    """A single USDT-perp listing on one exchange."""
    exchange: str
    base: str
    symbol: str               # unified ccxt symbol, e.g. "FOO/USDT:USDT"
    quote_volume_usd: float    # trailing 24h quote volume in USD (0.0 if unknown)
    active: bool = True


@dataclass(frozen=True)
class UniverseCandidate:
    """A base asset selected for collection, with its venues."""
    base: str
    execution_symbol: str
    execution_volume_usd: float
    detect_venues: dict[str, str]          # exchange -> symbol
    detect_volume_usd: dict[str, float]    # exchange -> 24h quote volume

    @property
    def n_detect_venues(self) -> int:
        return len(self.detect_venues)


def perp_infos_from_markets(
    exchange: str,
    markets: Iterable[Mapping],
    volumes: Optional[Mapping[str, float]] = None,
) -> dict[str, PerpInfo]:
    """Build `{base: PerpInfo}` from ccxt market dicts + an optional volume map.

    Keeps only active USDT-quoted linear perpetuals (ccxt ``type == 'swap'`` and
    ``linear``/settle == USDT). ``volumes`` maps unified symbol -> 24h quote
    volume (USD); missing entries default to 0.0. When two markets share a base
    (rare), the higher-volume one wins so we watch the liquid listing.
    """
    volumes = volumes or {}
    out: dict[str, PerpInfo] = {}
    for m in markets:
        if not m.get("swap"):
            continue
        if not m.get("active", True):
            continue
        # Linear USDT-settled only (skip inverse/coin-margined).
        if m.get("linear") is False:
            continue
        quote = m.get("quote")
        settle = m.get("settle")
        if quote not in STABLE_QUOTES and settle not in STABLE_QUOTES:
            continue
        base = m.get("base")
        if not base:
            continue
        symbol = m["symbol"]
        info = PerpInfo(
            exchange=exchange,
            base=base,
            symbol=symbol,
            quote_volume_usd=float(volumes.get(symbol, 0.0) or 0.0),
            active=bool(m.get("active", True)),
        )
        prev = out.get(base)
        if prev is None or info.quote_volume_usd > prev.quote_volume_usd:
            out[base] = info
    return out


def select_overlap_universe(
    perps: Mapping[str, Mapping[str, PerpInfo]],
    *,
    execution_exchange: str = "bybit",
    detect_exchanges: Optional[Iterable[str]] = None,
    min_volume_usd: float = 1_000_000.0,
    max_volume_usd: float = 50_000_000.0,
    exclude_bases: Iterable[str] = DEFAULT_EXCLUDE_BASES,
    limit: Optional[int] = None,
) -> list[UniverseCandidate]:
    """Select the illiquid-but-tradable cross-exchange overlap.

    Args:
        perps: ``{exchange: {base: PerpInfo}}`` for every venue.
        execution_exchange: venue we actually enter on (must list the base).
        detect_exchanges: venues we watch for walls. Defaults to every venue in
            ``perps`` except the execution venue.
        min_volume_usd / max_volume_usd: liquidity band on the execution venue.
            Below min = un-fillable; above max = a major we don't want.
        exclude_bases: hard exclusion list (big caps / stables).
        limit: cap the result to the N most illiquid candidates.

    Returns:
        Candidates sorted by detect-venue count (desc) then execution volume
        (asc — most illiquid first).
    """
    if execution_exchange not in perps:
        return []
    if detect_exchanges is None:
        detect_exchanges = [ex for ex in perps if ex != execution_exchange]
    detect_exchanges = [ex for ex in detect_exchanges if ex != execution_exchange]

    excluded = {b.upper() for b in exclude_bases} | {b.upper() for b in STABLE_BASES}
    exec_perps = perps[execution_exchange]

    candidates: list[UniverseCandidate] = []
    for base, ep in exec_perps.items():
        if base.upper() in excluded:
            continue
        if not ep.active:
            continue
        vol = ep.quote_volume_usd
        if vol < min_volume_usd or vol > max_volume_usd:
            continue

        detect_venues: dict[str, str] = {}
        detect_vols: dict[str, float] = {}
        for ex in detect_exchanges:
            dp = perps.get(ex, {}).get(base)
            if dp is not None and dp.active:
                detect_venues[ex] = dp.symbol
                detect_vols[ex] = dp.quote_volume_usd
        if not detect_venues:
            continue

        candidates.append(UniverseCandidate(
            base=base,
            execution_symbol=ep.symbol,
            execution_volume_usd=vol,
            detect_venues=detect_venues,
            detect_volume_usd=detect_vols,
        ))

    candidates.sort(key=lambda c: (-c.n_detect_venues, c.execution_volume_usd))
    if limit is not None:
        candidates = candidates[:limit]
    return candidates


# ---------------------------------------------------------------------------
# Thin network layer — turns a ccxt client into PerpInfo inputs.
# ---------------------------------------------------------------------------

def _quote_volumes_from_tickers(tickers: Mapping[str, Mapping[str, Any]]) -> dict[str, float]:
    """Extract ``{symbol: 24h quote volume USD}`` from a ccxt fetch_tickers map."""
    vols: dict[str, float] = {}
    for symbol, t in tickers.items():
        qv = t.get("quoteVolume")
        if qv is None:
            info = t.get("info") or {}
            # Fall back to common raw fields across venues.
            qv = info.get("turnover24h") or info.get("volValue") or info.get("amount24")
        try:
            vols[symbol] = float(qv) if qv is not None else 0.0
        except (TypeError, ValueError):
            vols[symbol] = 0.0
    return vols


async def fetch_perp_universe(adapter) -> dict[str, PerpInfo]:
    """Load one exchange's USDT-perp universe via an already-connected adapter.

    Reuses the universal ``CCXTAdapter`` — only its underlying ccxt ``client``
    (``markets`` + ``fetch_tickers``) is touched, no trading surface. Returns
    ``{base: PerpInfo}``. Volume failures degrade to 0.0 rather than raising, so
    the overlap still computes (a missing volume just won't pass the band).
    """
    client = adapter.client
    exchange_name = adapter.exchange.value
    try:
        tickers = await client.fetch_tickers()
    except Exception:
        tickers = {}
    volumes = _quote_volumes_from_tickers(tickers)
    return perp_infos_from_markets(exchange_name, client.markets.values(), volumes)
