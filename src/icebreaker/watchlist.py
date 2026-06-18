"""Watchlist config — which symbols to collect on which venues, runtime-editable.

Format (produced by icebreaker_preflight.py):

    {
      "_meta": {...},
      "symbols": {
        "BAN": {
          "enabled": true,
          "execution": {"bybit": "BAN/USDT:USDT"},
          "detect": {"kucoinfutures": "BAN/USDT:USDT"}
        },
        ...
      }
    }

We collect order book + trades on EVERY venue listed for a symbol (execution and
detect alike — the execution book is needed to measure the move, the detect book
to see the wall). ``subscriptions_by_exchange`` flattens the enabled symbols into
``{exchange: {ccxt_symbol, ...}}``; ``diff_subscriptions`` computes the
add/remove deltas when the file changes, so the collector re-subscribes without a
restart.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Watchlist:
    meta: dict = field(default_factory=dict)
    symbols: dict[str, dict] = field(default_factory=dict)

    def enabled_bases(self) -> list[str]:
        return [b for b, e in self.symbols.items() if e.get("enabled", True)]

    def subscriptions_by_exchange(self) -> dict[str, set[str]]:
        """``{exchange: {unified_symbol, ...}}`` over all enabled symbols."""
        subs: dict[str, set[str]] = {}
        for base, entry in self.symbols.items():
            if not entry.get("enabled", True):
                continue
            for group in ("execution", "detect"):
                for exchange, symbol in (entry.get(group) or {}).items():
                    subs.setdefault(exchange, set()).add(symbol)
        return subs


def load_watchlist(path: str | Path) -> Watchlist:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Watchlist(meta=data.get("_meta", {}), symbols=data.get("symbols", {}))


def diff_subscriptions(
    old: Mapping[str, set[str]],
    new: Mapping[str, set[str]],
) -> dict[str, dict[str, set[str]]]:
    """Per-exchange add/remove sets to move from ``old`` to ``new`` subscriptions.

    Returns ``{exchange: {"add": {...}, "remove": {...}}}``, only for exchanges
    with a non-empty delta.
    """
    out: dict[str, dict[str, set[str]]] = {}
    for exchange in set(old) | set(new):
        o = old.get(exchange, set())
        n = new.get(exchange, set())
        add = n - o
        remove = o - n
        if add or remove:
            out[exchange] = {"add": add, "remove": remove}
    return out
