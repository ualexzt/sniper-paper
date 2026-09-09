"""SQLite-to-dashboard adapter and read-only HTTP serving."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from .storage import Journal


def journal_dashboard(journal: Journal) -> dict[str, Any]:
    raw = journal.dashboard_snapshot()
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    last = raw["last_event"]
    events = raw["recent_events"]
    heartbeat = next((item for item in events if item["kind"] == "HEARTBEAT"), None)
    heartbeat_fresh = bool(heartbeat and now_ms - int(heartbeat["occurred_at_ms"]) <= 90_000)
    book_match = re.search(r"books (\d+)/(\d+)", heartbeat["message"] if heartbeat else "")
    ready_books = int(book_match.group(1)) if book_match else 0
    total_books = int(book_match.group(2)) if book_match else 0
    books_ready = heartbeat_fresh and total_books > 0 and ready_books == total_books
    websocket_connected = heartbeat_fresh and raw["meta"].get("stream_state") == "connected"
    service_status = "Healthy" if heartbeat_fresh else "Stale"
    data_status = "Fresh" if heartbeat_fresh else "Stale"
    universe_source = raw["universe"][0]["source"] if raw["universe"] else {}
    observation_only = not bool(universe_source.get("evaluation_eligible", False))
    mode = "Observation only" if observation_only else "Forward paper evaluation"
    return {
        "generated_at": now,
        "last_update": _stamp(last["occurred_at_ms"]) if last else "No events",
        "headline": f"{mode} · public market data · no order routing",
        "service_health": [
            {
                "label": "Paper service",
                "status": service_status,
                "detail": heartbeat["message"] if heartbeat else "Waiting for service heartbeat",
            }
        ],
        "data_health": [
            {
                "label": "WebSocket",
                "status": "Connected" if websocket_connected else "Disconnected",
                "detail": "Bybit public linear stream",
            },
            {
                "label": "Market books",
                "status": "Ready" if books_ready else ("Warming" if heartbeat_fresh else "Stale"),
                "detail": f"{ready_books}/{total_books} snapshots ready",
            },
            {
                "label": "SQLite journal",
                "status": data_status,
                "detail": str(journal.path),
            },
        ],
        "current_universe": [
            {
                "symbol": row["symbol"],
                "reason": _universe_reason(row["rank"], row["metrics"], mode),
                "spread_bp": f"{float(row['metrics'].get('spread_bps') or 0):.2f} bp",
                "depth": f"${float(row['metrics'].get('depth_notional_top5') or 0):,.0f}",
                "timeframe": "1m / 5m / 15m / 4h",
            }
            for row in raw["universe"]
        ],
        "active_positions": [
            {
                "symbol": row["symbol"],
                "side": row["side"],
                "entry": _number(row["entry_price"]),
                "stop": _number(row["stop_price"]),
                "target": _number(row["target_price"]),
                "mfe": f"{row['mfe_bp']:.2f} bp",
                "mae": f"{row['mae_bp']:.2f} bp",
                "pnl": "open",
                "opened_at": _stamp(row["opened_at_ms"]),
                "updated_at": now,
            }
            for row in raw["positions"]
        ],
        "recent_signals": [
            {
                "timestamp": _stamp(row["occurred_at_ms"]),
                "symbol": row["symbol"],
                "lane": row["lane"],
                "outcome": row["status"],
                "detail": row["reason"],
            }
            for row in raw["signals"]
        ],
        "lane_pnl": [
            {
                "lane": row["lane"],
                "pnl": f"{row['net_pnl']:.4f}",
                "trades": str(row["trades"]),
                "wins": str(row["wins"]),
                "losses": str(row["losses"]),
            }
            for row in raw["lane_pnl"]
        ],
    }


def _stamp(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _number(value: float) -> str:
    return f"{value:.10g}"


def _universe_reason(rank: int, metrics: dict[str, Any], mode: str) -> str:
    turnover = float(metrics.get("turnover24h") or 0)
    change = metrics.get("price_change_24h_pct")
    natr = metrics.get("natr_diagnostics") or {}
    natr_text = (
        f"NATR {float(natr['value']):.2f}%"
        if natr.get("available") and natr.get("value") is not None
        else f"NATR {natr.get('reason', 'unavailable')}"
    )
    change_text = f"{float(change):+.2f}%" if change is not None else "change unavailable"
    return (
        f"daily rank {rank} · turnover ${turnover:,.0f} · {change_text} · {natr_text} · {mode.lower()}"
    )
