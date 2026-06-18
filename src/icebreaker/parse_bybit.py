"""Pure parsers for Bybit v5 public WebSocket messages (orderbook + trades).

Kept separate from the connection/loop code so the wire format can be tested
without a socket. Each parser turns one raw message dict into normalized rows;
the collector adds ``recv_ts_ns`` (its own receive clock) on top — the parsers
only surface the exchange-side timestamp.

Bybit v5 reference:
  orderbook.<depth>.<SYM>  -> {topic,type:snapshot|delta,ts,data:{s,b,a,u,seq}}
  publicTrade.<SYM>        -> {topic,type,ts,data:[{T,s,S,v,p,i,...}]}
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OrderbookUpdate:
    symbol: str
    kind: str                       # "snapshot" | "delta"
    update_id: Optional[int]
    exch_ts_ms: int
    bids: list[tuple[float, float]]  # (price, size); size 0 = remove
    asks: list[tuple[float, float]]


@dataclass(frozen=True)
class TradeRow:
    symbol: str
    exch_ts_ms: int
    price: float
    qty: float
    side: str                       # "buy" | "sell" (taker aggressor)


def _levels(raw: Optional[list]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for lvl in raw or []:
        # Bybit sends ["price", "size"] as strings.
        out.append((float(lvl[0]), float(lvl[1])))
    return out


def parse_orderbook(msg: dict) -> Optional[OrderbookUpdate]:
    """Parse an ``orderbook.*`` message. Returns None for non-data frames."""
    topic = msg.get("topic", "")
    if not topic.startswith("orderbook."):
        return None
    data = msg.get("data")
    if not data:
        return None
    kind = msg.get("type")
    if kind not in ("snapshot", "delta"):
        return None
    u = data.get("u")
    return OrderbookUpdate(
        symbol=data.get("s", topic.rsplit(".", 1)[-1]),
        kind=kind,
        update_id=int(u) if u is not None else None,
        exch_ts_ms=int(msg.get("cts") or msg.get("ts") or 0),
        bids=_levels(data.get("b")),
        asks=_levels(data.get("a")),
    )


def parse_trades(msg: dict) -> list[TradeRow]:
    """Parse a ``publicTrade.*`` message into normalized trade rows."""
    topic = msg.get("topic", "")
    if not topic.startswith("publicTrade."):
        return []
    rows: list[TradeRow] = []
    for t in msg.get("data") or []:
        side_raw = (t.get("S") or "").lower()
        rows.append(TradeRow(
            symbol=t.get("s", topic.rsplit(".", 1)[-1]),
            exch_ts_ms=int(t.get("T", 0)),
            price=float(t["p"]),
            qty=float(t["v"]),
            side="buy" if side_raw == "buy" else "sell",
        ))
    return rows
