"""Append-oriented SQLite journal for paper decisions and outcomes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS universe_runs (
                    run_id TEXT PRIMARY KEY,
                    selected_at_ms INTEGER NOT NULL,
                    utc_date TEXT NOT NULL UNIQUE,
                    protocol_hash TEXT NOT NULL,
                    source_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS universe_members (
                    run_id TEXT NOT NULL REFERENCES universe_runs(run_id),
                    symbol TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    selected INTEGER NOT NULL CHECK(selected IN (0, 1)),
                    reason TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY,
                    occurred_at_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('LONG', 'SHORT')),
                    lane TEXT NOT NULL,
                    target_timeframe TEXT NOT NULL,
                    level_class TEXT NOT NULL,
                    trigger_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    protocol_hash TEXT NOT NULL,
                    features_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL UNIQUE REFERENCES signals(signal_id),
                    opened_at_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('LONG', 'SHORT')),
                    lane TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    quantity REAL NOT NULL CHECK(quantity > 0),
                    entry_fee REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('OPEN', 'CLOSED')),
                    mfe_bp REAL NOT NULL DEFAULT 0,
                    mae_bp REAL NOT NULL DEFAULT 0,
                    closed_at_ms INTEGER,
                    exit_price REAL,
                    exit_fee REAL,
                    exit_reason TEXT,
                    gross_pnl REAL,
                    net_pnl REAL
                );
                CREATE TABLE IF NOT EXISTS service_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at_ms INTEGER NOT NULL,
                    severity TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    opened_at_ms INTEGER NOT NULL,
                    closed_at_ms INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    delta_notional REAL NOT NULL,
                    trades INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY(symbol, timeframe, opened_at_ms)
                );
                CREATE INDEX IF NOT EXISTS idx_signals_time
                    ON signals(occurred_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_positions_status
                    ON positions(status, opened_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_service_events_time
                    ON service_events(occurred_at_ms DESC);
                """
            )
            db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def record_universe(
        self,
        *,
        run_id: str,
        selected_at_ms: int,
        utc_date: str,
        protocol_hash: str,
        source: Mapping[str, Any],
        members: Sequence[Mapping[str, Any]],
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO universe_runs VALUES (?, ?, ?, ?, ?)",
                (run_id, selected_at_ms, utc_date, protocol_hash, _json(source)),
            )
            db.executemany(
                """INSERT INTO universe_members
                   (run_id, symbol, rank, selected, reason, metrics_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        run_id,
                        str(member["symbol"]),
                        int(member["rank"]),
                        int(bool(member["selected"])),
                        str(member.get("reason", "")),
                        _json(member.get("metrics", {})),
                    )
                    for member in members
                ],
            )

    def record_signal(self, signal: Mapping[str, Any]) -> None:
        fields = (
            "signal_id",
            "occurred_at_ms",
            "symbol",
            "side",
            "lane",
            "target_timeframe",
            "level_class",
            "trigger_price",
            "stop_price",
            "target_price",
            "status",
            "reason",
            "protocol_hash",
        )
        values = [signal[field] for field in fields]
        values.append(_json(signal.get("features", {})))
        with self.connect() as db:
            db.execute(
                f"INSERT INTO signals ({', '.join(fields)}, features_json) VALUES ({', '.join('?' for _ in values)})",
                values,
            )

    def update_signal_status(self, signal_id: str, status: str, reason: str) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE signals SET status=?, reason=? WHERE signal_id=?",
                (status, reason, signal_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"unknown signal: {signal_id}")

    def open_position(self, position: Mapping[str, Any]) -> None:
        fields = (
            "position_id",
            "signal_id",
            "opened_at_ms",
            "symbol",
            "side",
            "lane",
            "entry_price",
            "stop_price",
            "target_price",
            "quantity",
            "entry_fee",
            "status",
        )
        with self.connect() as db:
            db.execute(
                f"INSERT INTO positions ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                [position[field] for field in fields],
            )

    def update_excursion(self, position_id: str, mfe_bp: float, mae_bp: float) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE positions SET mfe_bp=?, mae_bp=? WHERE position_id=? AND status='OPEN'",
                (mfe_bp, mae_bp, position_id),
            )

    def close_position(self, position_id: str, outcome: Mapping[str, Any]) -> None:
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE positions SET status='CLOSED', closed_at_ms=?,
                   exit_price=?, exit_fee=?, exit_reason=?, gross_pnl=?, net_pnl=?
                   WHERE position_id=? AND status='OPEN'""",
                (
                    outcome["closed_at_ms"],
                    outcome["exit_price"],
                    outcome["exit_fee"],
                    outcome["exit_reason"],
                    outcome["gross_pnl"],
                    outcome["net_pnl"],
                    position_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"position is not open: {position_id}")

    def event(
        self,
        occurred_at_ms: int,
        severity: str,
        kind: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO service_events
                   (occurred_at_ms, severity, kind, message, details_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (occurred_at_ms, severity, kind, message, _json(details or {})),
            )

    def record_bar(self, timeframe: str, bar: Any, source: str) -> None:
        self.record_bars(timeframe, [bar], source)

    def record_bars(self, timeframe: str, bars: Sequence[Any], source: str) -> None:
        if not bars:
            return
        with self.connect() as db:
            db.executemany(
                """INSERT OR IGNORE INTO bars
                   (symbol,timeframe,opened_at_ms,closed_at_ms,open,high,low,close,
                    volume,delta_notional,trades,source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        bar.symbol,
                        timeframe,
                        bar.opened_at_ms,
                        bar.closed_at_ms,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.delta_notional,
                        bar.trades,
                        source,
                    )
                    for bar in bars
                ],
            )

    def dashboard_snapshot(self, limit: int = 30) -> dict[str, Any]:
        with self.connect() as db:
            universe = db.execute(
                """SELECT ur.utc_date, ur.selected_at_ms, ur.source_json,
                          um.symbol, um.rank, um.metrics_json
                   FROM universe_runs ur JOIN universe_members um USING(run_id)
                   WHERE ur.utc_date=(SELECT MAX(utc_date) FROM universe_runs)
                     AND um.selected=1 ORDER BY um.rank"""
            ).fetchall()
            positions = db.execute("SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_at_ms").fetchall()
            signals = db.execute(
                "SELECT * FROM signals ORDER BY occurred_at_ms DESC LIMIT ?",
                (limit,),
            ).fetchall()
            lane_pnl = db.execute(
                """SELECT lane, COUNT(*) AS trades,
                   SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN net_pnl<=0 THEN 1 ELSE 0 END) AS losses,
                   COALESCE(SUM(net_pnl), 0) AS net_pnl
                   FROM positions WHERE status='CLOSED' GROUP BY lane ORDER BY lane"""
            ).fetchall()
            last_event = db.execute("SELECT * FROM service_events ORDER BY occurred_at_ms DESC LIMIT 1").fetchone()
            recent_events = db.execute("SELECT * FROM service_events ORDER BY occurred_at_ms DESC LIMIT 50").fetchall()
        universe_rows = []
        for row in universe:
            item = dict(row)
            item["source"] = json.loads(item.pop("source_json"))
            item["metrics"] = json.loads(item.pop("metrics_json"))
            universe_rows.append(item)
        return {
            "universe": universe_rows,
            "positions": [dict(row) for row in positions],
            "signals": [dict(row) for row in signals],
            "lane_pnl": [dict(row) for row in lane_pnl],
            "last_event": dict(last_event) if last_event else None,
            "recent_events": [dict(row) for row in recent_events],
        }

    def open_position_row(self) -> dict[str, Any] | None:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_at_ms").fetchall()
        if len(rows) > 1:
            raise RuntimeError("journal violates single-open-position invariant")
        return dict(rows[0]) if rows else None

    def universe_for_date(self, utc_date: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT um.* FROM universe_members um
                   JOIN universe_runs ur USING(run_id)
                   WHERE ur.utc_date=? AND um.selected=1 ORDER BY um.rank""",
                (utc_date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def universe_run_for_date(self, utc_date: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM universe_runs WHERE utc_date=?", (utc_date,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["source"] = json.loads(result.pop("source_json"))
        return result

    def attempted_level_lanes(self) -> set[tuple[str, str]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT lane, features_json FROM signals WHERE status IN ('TRIGGERED','OPEN','CLOSED','MISSED')"
            ).fetchall()
        attempts: set[tuple[str, str]] = set()
        for row in rows:
            features = json.loads(row["features_json"])
            level_id = features.get("level_id")
            if level_id:
                attempts.add((str(level_id), str(row["lane"])))
        return attempts

    def reconcile_orphaned_triggers(self, reason: str = "restart_before_paper_entry") -> int:
        """Mark memory-only pending attempts as missed after a restart."""
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE signals SET status='MISSED', reason=?
                   WHERE status='TRIGGERED'
                     AND signal_id NOT IN (SELECT signal_id FROM positions)""",
                (reason,),
            )
        return cursor.rowcount

    def signal_row(self, signal_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
        return dict(row) if row else None

    def realized_pnl(self) -> float:
        with self.connect() as db:
            value = db.execute("SELECT COALESCE(SUM(net_pnl), 0) FROM positions WHERE status='CLOSED'").fetchone()[0]
        return float(value)

    def daily_realized_pnl(self, timestamp_ms: int) -> float:
        day_ms = 86_400_000
        start = timestamp_ms // day_ms * day_ms
        with self.connect() as db:
            value = db.execute(
                """SELECT COALESCE(SUM(net_pnl), 0) FROM positions
                   WHERE status='CLOSED' AND closed_at_ms>=? AND closed_at_ms<?""",
                (start, start + day_ms),
            ).fetchone()[0]
        return float(value)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
