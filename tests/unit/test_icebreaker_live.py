"""Unit tests for the live Bybit client's pure helpers (Этап 1)."""
from __future__ import annotations

import json

from src.icebreaker.live_bybit import (
    batch_subscribe_frames,
    build_topics,
    unified_to_raw,
)


def test_unified_to_raw():
    assert unified_to_raw("BAN/USDT:USDT") == "BANUSDT"
    assert unified_to_raw("1000FLOKI/USDT:USDT") == "1000FLOKIUSDT"


def test_build_topics_orderbook_and_trades():
    topics = build_topics(["BAN/USDT:USDT", "ROSE/USDT:USDT"], depth=50)
    assert topics == [
        "orderbook.50.BANUSDT", "publicTrade.BANUSDT",
        "orderbook.50.ROSEUSDT", "publicTrade.ROSEUSDT",
    ]


def test_batch_subscribe_frames_respects_limit():
    topics = [f"t{i}" for i in range(25)]
    frames = list(batch_subscribe_frames(topics, batch=10))
    assert len(frames) == 3
    sizes = [len(json.loads(f)["args"]) for f in frames]
    assert sizes == [10, 10, 5]
    # every frame is a well-formed subscribe op
    for f in frames:
        assert json.loads(f)["op"] == "subscribe"


def test_batch_subscribe_frames_roundtrips_all_topics():
    topics = [f"t{i}" for i in range(23)]
    seen = []
    for f in batch_subscribe_frames(topics, batch=10):
        seen.extend(json.loads(f)["args"])
    assert seen == topics
