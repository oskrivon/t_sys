#!/usr/bin/env python3
"""Daily digest — health check + stats for all strategies.

Reads engine_state.db, docker container status, screener logs.
Sends summary to Telegram.

Usage:
    python scripts/daily_report.py            # print to console
    python scripts/daily_report.py --telegram  # send to Telegram

Cron (server):
    0 8 * * *  cd /root/trading && python3 scripts/daily_report.py --telegram >> data/logs/daily_report.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENGINE_DB = Path("data/engine_state.db")
PAPER_DB = Path("data/paper_trades.db")
ICEBREAKER_LOG = Path("data/icebreaker_paper_trades.jsonl")
ICEBREAKER_HB = Path("data/icebreaker_paper_heartbeat.json")

# Event-driven cron strategies: last-activity from their logs (informational, not alarms —
# these fire on schedule/events, so a stale log is normal, not a fault).
CRON_STRATEGY_LOGS = {
    "Weekend BTC": Path("data/logs/weekend.log"),
    "Calendar":    Path("data/logs/calendar.log"),
    "V-bottom":    Path("data/logs/v_bottom.log"),
}


# ------------------------------------------------------------------
# Funding Capture
# ------------------------------------------------------------------

def get_funding_report() -> str:
    """Funding capture stats from engine_state.db (last 24h + all time)."""
    if not ENGINE_DB.exists():
        return "--- FUNDING CAPTURE ---\nDB not found."

    conn = sqlite3.connect(str(ENGINE_DB))
    conn.row_factory = sqlite3.Row

    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # All closes
    all_closes = conn.execute(
        "SELECT pnl, metadata, qty FROM trades_log "
        "WHERE strategy_id='funding_capture' AND action='close'"
    ).fetchall()

    # Last 24h closes
    recent_closes = conn.execute(
        "SELECT pnl, metadata, symbol, timestamp, qty FROM trades_log "
        "WHERE strategy_id='funding_capture' AND action='close' AND timestamp > ?",
        (cutoff_24h,),
    ).fetchall()

    # Last trade timestamp
    last_trade = conn.execute(
        "SELECT timestamp FROM trades_log "
        "WHERE strategy_id='funding_capture' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    # Volume ranking paper targets
    vr_targets = conn.execute(
        "SELECT timestamp FROM trades_log "
        "WHERE strategy_id='volume_ranking' AND action='paper_target' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()

    conn.close()

    lines = ["--- FUNDING CAPTURE ---"]

    if last_trade:
        lines.append(f"Last trade: {last_trade['timestamp'][:16]}")
    else:
        lines.append("Last trade: NEVER")

    # 24h stats
    if recent_closes:
        pnls_24h = _parse_pnls(recent_closes)
        funding_24h = _sum_funding(recent_closes)
        lines.append(f"\nLast 24h: {len(recent_closes)} closes")
        if pnls_24h:
            wins = sum(1 for p in pnls_24h if p > 0)
            lines.append(f"  WR: {wins}/{len(pnls_24h)} ({wins/len(pnls_24h)*100:.0f}%)")
            lines.append(f"  Price PnL: {sum(pnls_24h):+.3f}%")
        lines.append(f"  Funding earned: ${funding_24h:.4f}")
    else:
        lines.append("\nLast 24h: 0 closes")
        # Check how long since last trade
        if last_trade:
            last_ts = datetime.fromisoformat(last_trade["timestamp"])
            hours_ago = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600
            if hours_ago > 12:
                lines.append(f"  WARNING: no trades in {hours_ago:.0f}h")

    # All time
    if all_closes:
        pnls_all = _parse_pnls(all_closes)
        funding_all = _sum_funding(all_closes)
        lines.append(f"\nAll time: {len(all_closes)} closes")
        if pnls_all:
            wins = sum(1 for p in pnls_all if p > 0)
            lines.append(f"  WR: {wins}/{len(pnls_all)} ({wins/len(pnls_all)*100:.0f}%)")
            lines.append(f"  Funding earned: ${funding_all:.4f}")

    return "\n".join(lines)


def _parse_pnls(rows) -> list[float]:
    result = []
    for r in rows:
        pnl = r["pnl"]
        if pnl and "%" in pnl:
            result.append(float(pnl.replace("%", "")))
    return result


def _sum_funding(rows) -> float:
    total = 0.0
    for r in rows:
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
            funding_bps = meta.get("funding_bps", 0)
            entry_price = float(meta.get("entry_price", 0) or 0)
            qty = float(meta.get("qty", 0) or 0)
            if not qty:
                # Try from row directly
                try:
                    qty = float(r["qty"] or 0)
                except (KeyError, TypeError, ValueError):
                    pass
            if funding_bps and entry_price and qty:
                total += funding_bps / 10000 * entry_price * qty
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return total


# ------------------------------------------------------------------
# Screener
# ------------------------------------------------------------------

def get_screener_report() -> str:
    """Screener health from docker logs (last 24h)."""
    lines = ["--- SCREENERS ---"]

    for name, container in [("4h", "miro-screener-4h"), ("1h", "miro-screener-1h")]:
        try:
            result = subprocess.run(
                ["docker", "logs", "--since", "24h", container],
                capture_output=True, text=True, timeout=10,
            )
            log_text = result.stdout + result.stderr

            scans = log_text.count("scan_complete")
            alerts = log_text.count("alert_sent")
            signals = sum(
                1 for line in log_text.split("\n")
                if "scan_complete" in line and "signals=0" not in line
            )
            errors = log_text.count("error")
            ml_filtered = log_text.count("signal_below_ml_threshold")

            lines.append(f"\n{name} screener:")
            lines.append(f"  Scans: {scans}, Signals found: {signals}, Alerts: {alerts}")
            if ml_filtered:
                lines.append(f"  ML filtered: {ml_filtered}")
            if errors > 0:
                lines.append(f"  Errors: {errors}")
            if scans == 0:
                lines.append("  WARNING: 0 scans in 24h!")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            lines.append(f"\n{name} screener: UNREACHABLE")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Docker Health
# ------------------------------------------------------------------

def get_docker_health() -> str:
    """Docker container status."""
    lines = ["--- INFRASTRUCTURE ---"]

    try:
        result = subprocess.run(
            ["docker", "compose", "-f", "docker-compose.platform.yml", "ps",
             "--format", "json"],
            capture_output=True, text=True, timeout=10,
        )
        # docker compose ps --format json outputs one JSON per line
        containers = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                try:
                    containers.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        if containers:
            for c in containers:
                name = c.get("Name", c.get("name", "?"))
                state = c.get("State", c.get("state", "?"))
                status = c.get("Status", c.get("status", "?"))
                icon = "OK" if state == "running" else "DOWN"
                lines.append(f"  [{icon}] {name}: {status}")
        else:
            # Fallback: plain text
            result2 = subprocess.run(
                ["docker", "compose", "-f", "docker-compose.platform.yml", "ps"],
                capture_output=True, text=True, timeout=10,
            )
            lines.append(result2.stdout[:500] if result2.stdout else "No containers")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        lines.append("  Docker not available")

    # Redis check
    try:
        result = subprocess.run(
            ["docker", "exec", "trading-redis", "redis-cli", "ping"],
            capture_output=True, text=True, timeout=5,
        )
        redis_ok = "PONG" in result.stdout
        lines.append(f"  Redis: {'OK' if redis_ok else 'DOWN'}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        lines.append("  Redis: UNKNOWN")

    # Engine restart count
    try:
        result = subprocess.run(
            ["docker", "logs", "--since", "24h", "trading-engine"],
            capture_output=True, text=True, timeout=10,
        )
        restarts = (result.stdout + result.stderr).count("engine_starting")
        if restarts > 3:
            lines.append(f"  WARNING: engine restarted {restarts}x in 24h")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "\n".join(lines)


# ------------------------------------------------------------------
# Volume Ranking
# ------------------------------------------------------------------

def get_volume_ranking_report() -> str:
    """Volume ranking paper targets from engine_state.db."""
    if not ENGINE_DB.exists():
        return "--- VOLUME RANKING ---\nDB not found."

    conn = sqlite3.connect(str(ENGINE_DB))
    conn.row_factory = sqlite3.Row

    # Last rebalance
    last = conn.execute(
        "SELECT timestamp FROM trades_log "
        "WHERE strategy_id='volume_ranking' AND action='paper_target' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()

    # Today's targets
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    targets = conn.execute(
        "SELECT symbol, side, metadata FROM trades_log "
        "WHERE strategy_id='volume_ranking' AND action='paper_target' "
        "AND timestamp LIKE ?",
        (f"{today}%",),
    ).fetchall()

    conn.close()

    lines = ["--- VOLUME RANKING (paper) ---"]

    if last:
        lines.append(f"Last rebalance: {last['timestamp'][:16]}")
    else:
        lines.append("Last rebalance: NEVER (waiting for first 00:05 UTC tick)")

    if targets:
        longs = [t for t in targets if t["side"] == "long"]
        shorts = [t for t in targets if t["side"] == "short"]
        lines.append(f"Today: {len(longs)}L / {len(shorts)}S")

        # Top 3 by score
        scored = []
        for t in targets:
            try:
                meta = json.loads(t["metadata"]) if t["metadata"] else {}
                scored.append((t["symbol"].split("/")[0], t["side"], meta.get("score", 0)))
            except (json.JSONDecodeError, ValueError):
                pass
        scored.sort(key=lambda x: x[2], reverse=True)
        if scored:
            top3 = ", ".join(f"{s[0]}({s[2]:.2f})" for s in scored[:3])
            bot3 = ", ".join(f"{s[0]}({s[2]:.2f})" for s in scored[-3:])
            lines.append(f"  Top vol accel: {top3}")
            lines.append(f"  Bottom: {bot3}")
    else:
        if not last:
            lines.append("Waiting for first daily tick...")
        else:
            lines.append("No targets today")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Icebreaker (paper daemon)
# ------------------------------------------------------------------

def get_icebreaker_report() -> str:
    """Liveness + paper P&L for the certified icebreaker daemon.

    Liveness comes from the heartbeat file the daemon rewrites every cycle — this is the
    only signal that distinguishes 'alive & scanning' from 'stuck/dead' during quiet periods
    (no signals, no errors ⇒ the trade log stays silent for days by design)."""
    lines = ["--- ICEBREAKER (paper) ---"]
    now = datetime.now(timezone.utc)

    hb = None
    if ICEBREAKER_HB.exists():
        try:
            hb = json.loads(ICEBREAKER_HB.read_text())
        except (json.JSONDecodeError, OSError):
            hb = None

    if hb:
        age = now.timestamp() - hb.get("ts", 0) / 1000
        alive = age < 180  # ~9 poll cycles at 20s; generous for occasional rate-limit stalls
        lines.append(
            f"  Daemon: [{'OK' if alive else 'STALE'}] beat {int(age)}s ago "
            f"(cycle {hb.get('cycle')}, {hb.get('scanned')}/{hb.get('universe')} scanned, "
            f"{hb.get('errors', 0)} err, {hb.get('cycle_s', '?')}s/cycle)"
        )
        if not alive:
            lines.append("  WARNING: heartbeat stale — daemon stuck or down!")
        open_syms = hb.get("open_syms") or []
        lines.append(f"  Open positions: {len(open_syms)}"
                     + (f" — {', '.join(open_syms)}" if open_syms else ""))
    else:
        # No heartbeat file: fall back to a process check so a fresh deploy still reports.
        try:
            r = subprocess.run(["pgrep", "-f", "run_icebreaker_paper"],
                               capture_output=True, text=True, timeout=5)
            alive = bool(r.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError):
            alive = False
        lines.append("  Daemon: process alive (heartbeat pending)" if alive
                     else "  Daemon: DOWN — no heartbeat and no process!")

    # Paper P&L from the append-only trade log (entry events + close records).
    entries, closes = [], []
    if ICEBREAKER_LOG.exists():
        for ln in ICEBREAKER_LOG.read_text(errors="ignore").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if d.get("event") == "entry":
                entries.append(d)
            elif "net" in d:
                closes.append(d)

    def _dt(s):
        try:
            return datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return None

    cutoff = now - timedelta(hours=24)
    ent_24 = [e for e in entries if (_dt(e.get("entry_time")) or now) > cutoff
              and _dt(e.get("entry_time")) is not None]
    cl_24 = [c for c in closes if (_dt(c.get("exit_time")) or now) > cutoff
             and _dt(c.get("exit_time")) is not None]

    if closes:
        nets = [c["net"] for c in closes]
        wr = sum(n > 0 for n in nets) / len(nets) * 100
        lines.append(f"  All time: {len(entries)} entries, {len(closes)} closes "
                     f"→ net {sum(nets)*100:+.2f}%, WR {wr:.0f}%, "
                     f"avg {sum(nets)/len(nets)*100:+.3f}%/trade")
    else:
        lines.append(f"  All time: {len(entries)} entries, 0 closes")

    lines.append(f"  Last 24h: {len(ent_24)} entries, {len(cl_24)} closes"
                 + (f" → net {sum(c['net'] for c in cl_24)*100:+.2f}%" if cl_24 else ""))

    if entries:
        last = max(entries, key=lambda e: e.get("entry_time", ""))
        lines.append(f"  Last signal: {(last.get('entry_time') or '?')[:16]} "
                     f"{last.get('sym')} {last.get('side')} mom {last.get('mom', 0)*100:.1f}%")
    else:
        lines.append("  No signals yet (rule fires ~9/mo ≈ 1 per 3 days)")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Cron strategies (event-driven — last activity only)
# ------------------------------------------------------------------

def get_cron_strategies_report() -> str:
    lines = ["--- STRATEGIES (cron) ---"]
    now = datetime.now(timezone.utc)
    for name, path in CRON_STRATEGY_LOGS.items():
        if not path.exists():
            lines.append(f"  {name}: no log yet")
            continue
        age_h = (now.timestamp() - path.stat().st_mtime) / 3600
        last = ""
        for ln in reversed(path.read_text(errors="ignore").splitlines()):
            if ln.strip():
                last = ln.strip()
                break
        lines.append(f"  {name}: last activity {age_h:.0f}h ago")
        if last:
            lines.append(f"    {last[:90]}")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Full Report
# ------------------------------------------------------------------

def get_full_report() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = f"=== Daily Digest {now} ==="

    sections = [
        header,
        get_docker_health(),
        get_icebreaker_report(),
        get_cron_strategies_report(),
        get_funding_report(),
        get_screener_report(),
        get_volume_ranking_report(),
    ]

    return "\n\n".join(sections)


async def send_telegram(text: str):
    """Send report to Telegram."""
    import os
    from dotenv import load_dotenv
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return

    from telegram import Bot
    bot = Bot(token=token)
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await bot.send_message(chat_id=chat_id, text=text[i:i+4000])
    else:
        await bot.send_message(chat_id=chat_id, text=text)
    print("Report sent to Telegram")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()

    report = get_full_report()
    print(report)

    if args.telegram:
        asyncio.run(send_telegram(report))


if __name__ == "__main__":
    main()
