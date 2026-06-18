"""Unit tests for Bybit WS parsers + watchlist (Этап 1)."""
from __future__ import annotations

import json

from src.icebreaker.book import BookState
from src.icebreaker.parse_bybit import parse_orderbook, parse_trades
from src.icebreaker.watchlist import (
    Watchlist,
    diff_subscriptions,
    load_watchlist,
)


# ---------------------------------------------------------------------------
# Bybit orderbook parser
# ---------------------------------------------------------------------------

def test_parse_orderbook_snapshot():
    msg = {
        "topic": "orderbook.50.BANUSDT",
        "type": "snapshot",
        "ts": 1672304484978,
        "data": {"s": "BANUSDT", "b": [["0.10", "1000"], ["0.09", "2000"]],
                 "a": [["0.11", "500"]], "u": 177400507, "seq": 7961638724},
    }
    ob = parse_orderbook(msg)
    assert ob.kind == "snapshot"
    assert ob.symbol == "BANUSDT"
    assert ob.update_id == 177400507
    assert ob.exch_ts_ms == 1672304484978
    assert ob.bids == [(0.10, 1000.0), (0.09, 2000.0)]
    assert ob.asks == [(0.11, 500.0)]


def test_parse_orderbook_delta_with_removal():
    msg = {
        "topic": "orderbook.50.BANUSDT", "type": "delta", "ts": 1,
        "data": {"s": "BANUSDT", "b": [["0.10", "0"]], "a": [], "u": 5},
    }
    ob = parse_orderbook(msg)
    assert ob.kind == "delta"
    assert ob.bids == [(0.10, 0.0)]   # 0 size = removal, preserved for BookState


def test_parse_orderbook_prefers_cts_when_present():
    msg = {"topic": "orderbook.50.X", "type": "delta", "ts": 200, "cts": 100,
           "data": {"s": "X", "b": [], "a": [], "u": 1}}
    assert parse_orderbook(msg).exch_ts_ms == 100


def test_parse_orderbook_ignores_non_orderbook():
    assert parse_orderbook({"topic": "tickers.BTCUSDT", "data": {}}) is None
    assert parse_orderbook({"topic": "orderbook.50.X", "type": "delta"}) is None  # no data


def test_parser_feeds_bookstate_end_to_end():
    """Raw Bybit frames -> parser -> BookState produces a correct book + wall."""
    snap = {"topic": "orderbook.50.X", "type": "snapshot", "ts": 1,
            "data": {"s": "X", "b": [["10.0", "20000"]], "a": [["11.0", "5"]], "u": 100}}
    delta = {"topic": "orderbook.50.X", "type": "delta", "ts": 2,
             "data": {"s": "X", "b": [["10.0", "1"]], "a": [], "u": 101}}
    book = BookState()
    ob1 = parse_orderbook(snap)
    book.apply_snapshot(ob1.bids, ob1.asks, ob1.update_id)
    assert len(book.walls(100_000)) == 1          # 10 * 20000 = 200k wall
    ob2 = parse_orderbook(delta)
    book.apply_delta(ob2.bids, ob2.asks, ob2.update_id)
    assert book.walls(100_000) == []              # wall eaten down to size 1


# ---------------------------------------------------------------------------
# Bybit trade parser
# ---------------------------------------------------------------------------

def test_parse_trades():
    msg = {
        "topic": "publicTrade.BANUSDT", "type": "snapshot", "ts": 1,
        "data": [
            {"T": 1672304486865, "s": "BANUSDT", "S": "Buy", "v": "100", "p": "0.105"},
            {"T": 1672304486870, "s": "BANUSDT", "S": "Sell", "v": "50", "p": "0.104"},
        ],
    }
    rows = parse_trades(msg)
    assert len(rows) == 2
    assert rows[0].side == "buy" and rows[0].price == 0.105 and rows[0].qty == 100.0
    assert rows[1].side == "sell" and rows[1].exch_ts_ms == 1672304486870


def test_parse_trades_ignores_other_topics():
    assert parse_trades({"topic": "orderbook.50.X", "data": []}) == []


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def _wl() -> Watchlist:
    return Watchlist(symbols={
        "BAN": {"enabled": True, "execution": {"bybit": "BAN/USDT:USDT"},
                "detect": {"kucoinfutures": "BAN/USDT:USDT"}},
        "ROSE": {"enabled": False, "execution": {"bybit": "ROSE/USDT:USDT"},
                 "detect": {"kucoinfutures": "ROSE/USDT:USDT"}},
    })


def test_enabled_bases_skips_disabled():
    assert _wl().enabled_bases() == ["BAN"]


def test_subscriptions_by_exchange_flattens_enabled_only():
    subs = _wl().subscriptions_by_exchange()
    assert subs == {
        "bybit": {"BAN/USDT:USDT"},
        "kucoinfutures": {"BAN/USDT:USDT"},
    }
    # ROSE disabled -> not present


def test_load_watchlist_roundtrip(tmp_path):
    p = tmp_path / "wl.json"
    p.write_text(json.dumps({
        "_meta": {"execution_exchange": "bybit"},
        "symbols": {"BAN": {"enabled": True, "execution": {"bybit": "BAN/USDT:USDT"},
                            "detect": {}}},
    }), encoding="utf-8")
    wl = load_watchlist(p)
    assert wl.meta["execution_exchange"] == "bybit"
    assert wl.enabled_bases() == ["BAN"]


def test_diff_subscriptions_add_remove():
    old = {"bybit": {"A/USDT:USDT", "B/USDT:USDT"}}
    new = {"bybit": {"B/USDT:USDT", "C/USDT:USDT"},
           "mexc": {"C/USDT:USDT"}}
    d = diff_subscriptions(old, new)
    assert d["bybit"]["add"] == {"C/USDT:USDT"}
    assert d["bybit"]["remove"] == {"A/USDT:USDT"}
    assert d["mexc"]["add"] == {"C/USDT:USDT"}
    assert d["mexc"]["remove"] == set()


def test_diff_subscriptions_no_change_is_empty():
    subs = {"bybit": {"A/USDT:USDT"}}
    assert diff_subscriptions(subs, dict(subs)) == {}
