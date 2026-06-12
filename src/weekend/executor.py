"""Live execution for weekend strategy — opens/closes BTC positions on exchanges."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import structlog

logger = structlog.get_logger()


@dataclass
class FillResult:
    """Result of a single exchange fill."""
    exchange: str
    side: str
    qty: float
    avg_price: float
    order_id: str
    sl_order_id: Optional[str] = None


def _load_exchange(exchange_id: str):
    """Create authenticated ccxt exchange instance."""
    import ccxt
    from dotenv import load_dotenv
    load_dotenv()

    if exchange_id == "bybit":
        return ccxt.bybit({
            "apiKey": os.getenv("BYBIT_API_KEY"),
            "secret": os.getenv("BYBIT_API_SECRET"),
        })
    elif exchange_id == "binance":
        return ccxt.binance({
            "apiKey": os.getenv("BINANCE_API_KEY"),
            "secret": os.getenv("BINANCE_API_SECRET"),
            "options": {"defaultType": "swap"},
        })
    else:
        raise ValueError(f"Unknown exchange: {exchange_id}")


def _set_leverage(exchange, symbol: str, leverage: int) -> None:
    """Set leverage, ignore 'not modified' errors."""
    try:
        exchange.set_leverage(leverage, symbol)
    except Exception as e:
        if "not modified" not in str(e).lower():
            logger.warning("set_leverage_warning", exchange=exchange.id,
                           symbol=symbol, error=str(e))


def _compute_qty(exchange, symbol: str, notional: float, leverage: int) -> tuple[float, float]:
    """Compute order qty from notional and current price."""
    ticker = exchange.fetch_ticker(symbol)
    price = ticker["last"]
    raw_qty = (notional * leverage) / price
    # Round to market precision
    market = exchange.market(symbol)
    precision = market.get("precision", {}).get("amount", 8)
    if isinstance(precision, int) and precision > 1:
        # Binance-style: number of decimal places
        qty = round(raw_qty, precision)
    else:
        # Bybit-style: step size
        step = float(market.get("precision", {}).get("amount", 0.001))
        qty = round(raw_qty / step) * step if step > 0 else raw_qty
        qty = round(qty, 8)
    # Ensure minimum order size
    min_qty = float(market.get("limits", {}).get("amount", {}).get("min", 0))
    if min_qty and qty < min_qty:
        qty = min_qty
    return qty, price


def open_position(
    exchange_id: str,
    symbol: str,
    direction: str,
    notional: float,
    leverage: int,
    sl_price: float,
    margin_reserve_pct: float = 0.05,
) -> Optional[FillResult]:
    """Open BTC position on one exchange with SL order.

    Args:
        exchange_id: "bybit" or "binance"
        symbol: e.g. "BTC/USDT:USDT"
        direction: "long" or "short"
        notional: USD margin per exchange. 0 = use full available balance.
        leverage: leverage multiplier
        sl_price: stop-loss trigger price
        margin_reserve_pct: keep this fraction of balance as buffer (default 5%)

    Returns:
        FillResult on success, None on failure.
    """
    try:
        exchange = _load_exchange(exchange_id)
        side = "buy" if direction == "long" else "sell"
        sl_side = "sell" if direction == "long" else "buy"

        _set_leverage(exchange, symbol, leverage)

        # Dynamic notional: use full balance if notional=0
        if notional <= 0:
            bal = exchange.fetch_balance()
            free = float(bal.get("USDT", {}).get("free", 0))
            notional = free * (1 - margin_reserve_pct)
            logger.info("weekend_dynamic_notional",
                        exchange=exchange_id, free=free, notional=notional)

        qty, price = _compute_qty(exchange, symbol, notional, leverage)

        logger.info("weekend_opening", exchange=exchange_id, symbol=symbol,
                    side=side, qty=qty, notional=notional, leverage=leverage,
                    price=price)

        # Market entry
        order = exchange.create_order(symbol, "market", side, qty)
        avg_price = order.get("average") or order.get("price") or price
        order_id = order.get("id", "")

        logger.info("weekend_opened", exchange=exchange_id,
                    avg_price=avg_price, qty=qty, order_id=order_id)

        # Place SL conditional order
        sl_order_id = None
        try:
            if exchange_id == "bybit":
                sl_order = exchange.create_order(
                    symbol, "market", sl_side, qty,
                    params={
                        "triggerPrice": sl_price,
                        "triggerDirection": 2 if direction == "long" else 1,
                        "reduceOnly": True,
                    },
                )
            else:
                # Binance futures: stop market
                sl_order = exchange.create_order(
                    symbol, "market", sl_side, qty,
                    params={
                        "stopPrice": sl_price,
                        "type": "STOP_MARKET",
                        "reduceOnly": True,
                    },
                )
            sl_order_id = sl_order.get("id", "")
            logger.info("weekend_sl_placed", exchange=exchange_id,
                        sl_price=sl_price, sl_order_id=sl_order_id)
        except Exception as e:
            logger.error("weekend_sl_failed", exchange=exchange_id,
                         error=str(e))

        return FillResult(
            exchange=exchange_id,
            side=side,
            qty=qty,
            avg_price=float(avg_price),
            order_id=str(order_id),
            sl_order_id=str(sl_order_id) if sl_order_id else None,
        )

    except Exception as e:
        logger.error("weekend_open_failed", exchange=exchange_id, error=str(e))
        return None


def close_position(exchange_id: str, symbol: str, direction: str) -> Optional[FillResult]:
    """Close BTC position on one exchange. Cancels open SL orders first.

    Args:
        exchange_id: "bybit" or "binance"
        symbol: e.g. "BTC/USDT:USDT"
        direction: original direction ("long" or "short")

    Returns:
        FillResult on success, None on failure or no position.
    """
    try:
        exchange = _load_exchange(exchange_id)
        close_side = "sell" if direction == "long" else "buy"

        # Find open position qty
        positions = exchange.fetch_positions([symbol])
        open_qty = 0.0
        for pos in positions:
            q = abs(float(pos.get("contracts", 0)))
            if q > 0:
                open_qty = q
                break

        if open_qty == 0:
            logger.info("weekend_no_position", exchange=exchange_id, symbol=symbol)
            return None

        # Cancel any open SL orders — both regular and conditional/algo buckets.
        # Binance keeps STOP_MARKET reduceOnly orders in a separate "conditional"
        # bucket invisible to a plain fetch_open_orders; without the stop=True
        # query the SL is orphaned after close and can fire on a later position.
        orders_to_cancel: dict[str, dict] = {}
        for params in ({}, {"stop": True}):
            try:
                for o in exchange.fetch_open_orders(symbol, params=params):
                    orders_to_cancel[o["id"]] = params
            except Exception:
                pass
        for oid, params in orders_to_cancel.items():
            try:
                exchange.cancel_order(oid, symbol, params=params)
                logger.info("weekend_sl_cancelled", exchange=exchange_id,
                            order_id=oid)
            except Exception:
                pass

        # Market close
        order = exchange.create_order(
            symbol, "market", close_side, open_qty,
            params={"reduceOnly": True},
        )
        avg_price = order.get("average") or order.get("price") or 0
        order_id = order.get("id", "")

        logger.info("weekend_closed", exchange=exchange_id,
                    avg_price=avg_price, qty=open_qty, order_id=order_id)

        return FillResult(
            exchange=exchange_id,
            side=close_side,
            qty=open_qty,
            avg_price=float(avg_price),
            order_id=str(order_id),
        )

    except Exception as e:
        logger.error("weekend_close_failed", exchange=exchange_id, error=str(e))
        return None


def check_position(exchange_id: str, symbol: str) -> dict:
    """Check current position on exchange. Returns {qty, side, upnl}."""
    try:
        exchange = _load_exchange(exchange_id)
        positions = exchange.fetch_positions([symbol])
        for pos in positions:
            q = abs(float(pos.get("contracts", 0)))
            if q > 0:
                return {
                    "exchange": exchange_id,
                    "qty": q,
                    "side": pos.get("side"),
                    "upnl": float(pos.get("unrealizedPnl", 0)),
                    "entry_price": float(pos.get("entryPrice", 0)),
                    "mark_price": float(pos.get("markPrice", 0)),
                }
        return {"exchange": exchange_id, "qty": 0}
    except Exception as e:
        logger.error("weekend_check_failed", exchange=exchange_id, error=str(e))
        return {"exchange": exchange_id, "qty": 0, "error": str(e)}
