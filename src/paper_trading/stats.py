"""Paper trading statistics."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import db


@dataclass
class PaperStats:
    total: int = 0
    open: int = 0
    closed: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    total_pnl_pct: float = 0.0
    expectancy_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    profit_factor: float = 0.0

    def format(self) -> str:
        lines = [
            "=== Paper Trading Stats ===",
            f"Total trades:  {self.total} ({self.open} open, {self.closed} closed)",
            f"Wins / Losses: {self.wins} / {self.losses}",
            f"Win rate:      {self.win_rate:.1f}%",
            f"Avg win:       +{self.avg_win_pct:.2f}%",
            f"Avg loss:      {self.avg_loss_pct:.2f}%",
            f"Expectancy:    {self.expectancy_pct:+.2f}% per trade",
            f"Total P&L:     {self.total_pnl_pct:+.2f}%",
            f"Profit factor: {self.profit_factor:.2f}",
            f"Best trade:    {self.best_trade_pct:+.2f}%",
            f"Worst trade:   {self.worst_trade_pct:+.2f}%",
        ]
        return "\n".join(lines)


def compute_stats(conn: sqlite3.Connection) -> PaperStats:
    """Compute stats from all trades in DB."""
    all_trades = db.get_all_trades(conn)
    closed = [t for t in all_trades if t["status"] != "open"]
    open_trades = [t for t in all_trades if t["status"] == "open"]

    if not closed:
        return PaperStats(total=len(all_trades), open=len(open_trades))

    wins = [t for t in closed if t["pnl_pct"] and t["pnl_pct"] > 0]
    losses = [t for t in closed if t["pnl_pct"] and t["pnl_pct"] <= 0]

    win_pnls = [t["pnl_pct"] for t in wins]
    loss_pnls = [t["pnl_pct"] for t in losses]
    all_pnls = [t["pnl_pct"] for t in closed if t["pnl_pct"] is not None]

    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
    total_pnl = sum(all_pnls)

    gross_profit = sum(p for p in all_pnls if p > 0)
    gross_loss = abs(sum(p for p in all_pnls if p < 0))

    return PaperStats(
        total=len(all_trades),
        open=len(open_trades),
        closed=len(closed),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(closed) * 100 if closed else 0,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        total_pnl_pct=total_pnl,
        expectancy_pct=total_pnl / len(closed) if closed else 0,
        best_trade_pct=max(all_pnls) if all_pnls else 0,
        worst_trade_pct=min(all_pnls) if all_pnls else 0,
        profit_factor=gross_profit / gross_loss if gross_loss > 0 else float("inf"),
    )


def format_open_trades(conn: sqlite3.Connection) -> str:
    """Format open trades as a readable table."""
    trades = db.get_open_trades(conn)
    if not trades:
        return "No open trades."

    lines = [f"=== Open Trades ({len(trades)}) ==="]
    for t in trades:
        ml = f"ML:{t['ml_score']:.0%}" if t["ml_score"] else ""
        vol = f"Vol:{t['volume_ratio']:.1f}x" if t["volume_ratio"] else ""
        lines.append(
            f"  #{t['id']} {t['symbol']} {t['direction'].upper()} "
            f"@ {t['entry_price']:.8g}  "
            f"SL:{t['sl']:.8g} TP:{t['tp']:.8g}  "
            f"{ml} {vol}  "
            f"({t['signal_time'][:16]})"
        )
    return "\n".join(lines)


def format_recent_trades(conn: sqlite3.Connection, limit: int = 10) -> str:
    """Format recent closed trades."""
    closed = db.get_closed_trades(conn)[:limit]
    if not closed:
        return "No closed trades yet."

    lines = [f"=== Recent Closed Trades (last {limit}) ==="]
    for t in closed:
        icon = "W" if t["pnl_pct"] and t["pnl_pct"] > 0 else "L"
        pnl = f"{t['pnl_pct']:+.2f}%" if t["pnl_pct"] is not None else "?"
        lines.append(
            f"  [{icon}] #{t['id']} {t['symbol']} {t['direction'].upper()} "
            f"@ {t['entry_price']:.8g} -> {t['close_price']:.8g}  "
            f"{pnl} ({t['status']})"
        )
    return "\n".join(lines)
