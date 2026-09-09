"""Append-oriented SQLite journal for paper decisions and outcomes."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5


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
                CREATE TABLE IF NOT EXISTS paper_orders (
                    order_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL UNIQUE REFERENCES signals(signal_id),
                    created_at_ms INTEGER NOT NULL,
                    activated_at_ms INTEGER,
                    missed_at_ms INTEGER,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('LONG', 'SHORT')),
                    lane TEXT NOT NULL,
                    entry_price REAL,
                    quantity REAL NOT NULL DEFAULT 0,
                    filled_qty REAL NOT NULL DEFAULT 0,
                    queue_ahead_qty REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'ACTIVE', 'PARTIAL', 'FILLED', 'MISSED', 'CANCELLED')),
                    miss_reason TEXT,
                    last_quote_received_at_ms INTEGER,
                    last_bid REAL,
                    last_ask REAL,
                    last_bid_size REAL,
                    last_ask_size REAL
                );
                CREATE TABLE IF NOT EXISTS service_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at_ms INTEGER NOT NULL,
                    severity TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_diagnostics (
                    diagnostic_id TEXT PRIMARY KEY,
                    setup_id TEXT NOT NULL,
                    occurred_at_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    side TEXT CHECK(side IS NULL OR side IN ('LONG', 'SHORT')),
                    status TEXT NOT NULL CHECK(status IN (
                        'OBSERVED', 'ARMED', 'CONFIRMED', 'BLOCKED', 'REJECTED', 'INVALIDATED', 'EXPIRED'
                    )),
                    reason TEXT NOT NULL,
                    reference_price REAL,
                    stop_price REAL,
                    target_price REAL,
                    protocol_hash TEXT NOT NULL,
                    features_json TEXT NOT NULL
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
                -- The level catalog is deliberately separate from bars and
                -- execution tables.  A level_id identifies one formation;
                -- revisions change its description without replacing its
                -- identity or losing a previously observed break.
                CREATE TABLE IF NOT EXISTS levels (
                    level_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    version TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('HIGH', 'LOW')),
                    price REAL NOT NULL,
                    zone_low REAL NOT NULL,
                    zone_high REAL NOT NULL,
                    touches INTEGER NOT NULL CHECK(touches >= 0),
                    level_class TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    origin_at_ms INTEGER NOT NULL,
                    confirmed_at_ms INTEGER NOT NULL,
                    first_seen_at_ms INTEGER NOT NULL,
                    broken_at_ms INTEGER,
                    invalidation_reason TEXT,
                    updated_at_ms INTEGER NOT NULL,
                    CHECK(zone_low <= zone_high),
                    CHECK(origin_at_ms <= confirmed_at_ms),
                    CHECK(first_seen_at_ms >= confirmed_at_ms),
                    CHECK(broken_at_ms IS NULL OR broken_at_ms >= confirmed_at_ms)
                );
                CREATE TABLE IF NOT EXISTS level_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    version TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('HIGH', 'LOW')),
                    price REAL NOT NULL,
                    zone_low REAL NOT NULL,
                    zone_high REAL NOT NULL,
                    touches INTEGER NOT NULL CHECK(touches >= 0),
                    level_class TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    origin_at_ms INTEGER NOT NULL,
                    confirmed_at_ms INTEGER NOT NULL,
                    first_seen_at_ms INTEGER NOT NULL,
                    broken_at_ms INTEGER,
                    invalidation_reason TEXT,
                    updated_at_ms INTEGER NOT NULL,
                    archived_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_signals_time
                    ON signals(occurred_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_positions_status
                    ON positions(status, opened_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_paper_orders_status
                    ON paper_orders(status, created_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_service_events_time
                    ON service_events(occurred_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_shadow_diagnostics_symbol_time
                    ON shadow_diagnostics(symbol, occurred_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_levels_symbol_timeframe
                    ON levels(symbol, timeframe, updated_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_level_history_level_revision
                    ON level_history(level_id, revision, history_id DESC);
                -- This is an independent diagnostic path.  signal_id is not
                -- an FK because rejected/missed candidates can be recorded
                -- before (or without) an execution signal row.
                CREATE TABLE IF NOT EXISTS signal_path_events (
                    event_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    occurred_at_ms INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    price REAL,
                    reference_price REAL,
                    tp_touched TEXT NOT NULL CHECK(tp_touched IN ('YES', 'NO', 'AMBIGUOUS', 'UNKNOWN')),
                    sl_touched TEXT NOT NULL CHECK(sl_touched IN ('YES', 'NO', 'AMBIGUOUS', 'UNKNOWN')),
                    mfe_bp REAL,
                    mae_bp REAL,
                    coverage REAL CHECK(coverage IS NULL OR (coverage >= 0 AND coverage <= 1)),
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_signal_path_signal_time
                    ON signal_path_events(signal_id, occurred_at_ms, event_id);
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

    def update_open_position(self, position_id: str, *, quantity: float, entry_price: float, entry_fee: float) -> None:
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE positions SET quantity=?, entry_price=?, entry_fee=?
                   WHERE position_id=? AND status='OPEN'""",
                (quantity, entry_price, entry_fee, position_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"position is not open: {position_id}")

    def record_paper_order(self, order: Mapping[str, Any]) -> None:
        fields = (
            "order_id",
            "signal_id",
            "created_at_ms",
            "activated_at_ms",
            "missed_at_ms",
            "symbol",
            "side",
            "lane",
            "entry_price",
            "quantity",
            "filled_qty",
            "queue_ahead_qty",
            "status",
            "miss_reason",
            "last_quote_received_at_ms",
            "last_bid",
            "last_ask",
            "last_bid_size",
            "last_ask_size",
        )
        with self.connect() as db:
            db.execute(
                f"INSERT INTO paper_orders ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                [order.get(field) for field in fields],
            )

    def update_paper_order(self, order_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "activated_at_ms",
            "missed_at_ms",
            "entry_price",
            "quantity",
            "filled_qty",
            "queue_ahead_qty",
            "status",
            "miss_reason",
            "last_quote_received_at_ms",
            "last_bid",
            "last_ask",
            "last_bid_size",
            "last_ask_size",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported paper order fields: {sorted(unknown)}")
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [order_id]
        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE paper_orders SET {assignments} WHERE order_id=?",
                values,
            )
            if cursor.rowcount != 1:
                raise ValueError(f"unknown paper order: {order_id}")

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

    def record_shadow_diagnostic(self, diagnostic: Mapping[str, Any]) -> bool:
        """Persist one idempotent observation with no paper-execution linkage."""
        fields = (
            "diagnostic_id",
            "setup_id",
            "occurred_at_ms",
            "symbol",
            "lane",
            "side",
            "status",
            "reason",
            "reference_price",
            "stop_price",
            "target_price",
            "protocol_hash",
        )
        values = [diagnostic.get(field) for field in fields]
        values.append(_json(diagnostic.get("features", {})))
        with self.connect() as db:
            cursor = db.execute(
                f"INSERT INTO shadow_diagnostics "
                f"({', '.join(fields)}, features_json) VALUES ({', '.join('?' for _ in values)}) "
                "ON CONFLICT(diagnostic_id) DO NOTHING",
                values,
            )
        return cursor.rowcount == 1

    def shadow_diagnostics(
        self,
        symbol: str | None = None,
        limit: int = 100,
        *,
        protocol_hash: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        with self.connect() as db:
            clauses = []
            values: list[Any] = []
            if symbol is not None:
                clauses.append("symbol=?")
                values.append(symbol)
            if protocol_hash is not None:
                clauses.append("protocol_hash=?")
                values.append(protocol_hash)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            values.append(limit)
            rows = db.execute(
                f"SELECT * FROM shadow_diagnostics{where} ORDER BY occurred_at_ms DESC LIMIT ?",
                values,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["features"] = json.loads(item.pop("features_json"))
            result.append(item)
        return result

    def record_signal_path_event(self, event: Mapping[str, Any]) -> bool:
        """Append one signal-path diagnostic event idempotently.

        ``event_id`` is optional; when omitted, ``signal_id``, timestamp and
        event type form the stable identity.  A repeated identity is ignored,
        preserving the first observation and keeping this journal append-only.
        This table intentionally has no FK to ``signals`` because diagnostics
        also describe rejected, missed, or otherwise non-execution paths.
        """
        row = _normalise_signal_path_event(event)
        fields = _SIGNAL_PATH_FIELDS
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO signal_path_events ("
                + ", ".join(fields)
                + ") VALUES ("
                + ", ".join("?" for _ in fields)
                + ") ON CONFLICT(event_id) DO NOTHING",
                [row[field] for field in fields],
            )
        return cursor.rowcount == 1

    def signal_path_event(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM signal_path_events WHERE event_id=?", (event_id,)).fetchone()
        return _signal_path_mapping(row) if row is not None else None

    def signal_path_events(
        self,
        signal_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read path events in causal order (timestamp, then stable identity)."""
        if limit < 1:
            return []
        values: list[Any] = [signal_id] if signal_id is not None else []
        where = " WHERE signal_id=?" if signal_id is not None else ""
        values.append(limit)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM signal_path_events" + where + " ORDER BY occurred_at_ms, event_id LIMIT ?",
                values,
            ).fetchall()
        return [_signal_path_mapping(row) for row in rows]

    def signal_path_events_for_symbol(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return newest path diagnostics for one market, joined read-only to signals."""
        if limit < 1:
            return []
        with self.connect() as db:
            rows = db.execute(
                """SELECT path.*, signals.symbol, signals.lane
                   FROM signal_path_events AS path
                   JOIN signals USING(signal_id)
                   WHERE signals.symbol=?
                   ORDER BY path.occurred_at_ms DESC, path.event_id DESC
                   LIMIT ?""",
                (symbol, limit),
            ).fetchall()
        return [_signal_path_mapping(row) for row in rows]

    # Concise alias for callers that use the journal as a signal-path store.
    record_signal_path = record_signal_path_event

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

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

    def upsert_level(self, level: Mapping[str, Any]) -> dict[str, Any]:
        """Insert or update one canonical level and return its stored mapping.

        The operation is safe to repeat.  A previously stored break is
        monotonic: a later reclustering/bootstrap snapshot cannot clear it.
        Before changing an existing row its prior state is copied to
        ``level_history`` so revisions remain auditable.
        """
        incoming = _normalise_level(level)
        fields = _LEVEL_STORAGE_FIELDS
        with self.connect() as db:
            existing = db.execute(
                "SELECT " + ", ".join(fields) + " FROM levels WHERE level_id=?",
                (incoming["level_id"],),
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO levels (" + ", ".join(fields) + ") VALUES (" + ", ".join("?" for _ in fields) + ")",
                    [incoming[field] for field in fields],
                )
                stored = incoming
            else:
                merged = _merge_level(existing, incoming)
                stored = dict(existing)
                if any(stored[field] != merged[field] for field in fields):
                    archived_at_ms = max(int(existing["updated_at_ms"]), int(merged["updated_at_ms"]))
                    history_fields = fields + ("archived_at_ms",)
                    db.execute(
                        "INSERT INTO level_history ("
                        + ", ".join(history_fields)
                        + ") VALUES ("
                        + ", ".join("?" for _ in history_fields)
                        + ")",
                        [existing[field] for field in fields] + [archived_at_ms],
                    )
                    assignments = ", ".join(f"{field}=?" for field in fields if field != "level_id")
                    db.execute(
                        "UPDATE levels SET " + assignments + " WHERE level_id=?",
                        [merged[field] for field in fields if field != "level_id"] + [merged["level_id"]],
                    )
                    stored = merged
        return _level_mapping(stored)

    def upsert_levels(self, levels: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Upsert levels in one caller-defined order and return stored rows."""
        return [self.upsert_level(level) for level in levels]

    def level_row(self, level_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM levels WHERE level_id=?", (level_id,)).fetchone()
        return _level_mapping(row) if row is not None else None

    def load_levels(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
        *,
        version: str | None = None,
        include_broken: bool = True,
        as_of_ms: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Load canonical level mappings, optionally at a causal timestamp.

        ``as_of_ms`` uses the same half-open lifecycle as the strategy: a
        level is visible after confirmation and remains active strictly before
        its break timestamp.  With the default ``include_broken=True`` all
        canonical rows are returned, including historical broken levels.
        """
        clauses: list[str] = []
        values: list[Any] = []
        if symbol is not None:
            clauses.append("symbol=?")
            values.append(symbol)
        if timeframe is not None:
            clauses.append("timeframe=?")
            values.append(timeframe)
        if version is not None:
            clauses.append("version=?")
            values.append(version)
        if not include_broken:
            clauses.append("broken_at_ms IS NULL")
        if as_of_ms is not None:
            clauses.extend(("confirmed_at_ms<=?", "(broken_at_ms IS NULL OR broken_at_ms>?)"))
            values.extend((as_of_ms, as_of_ms))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        suffix = ""
        if limit is not None:
            if limit < 1:
                return []
            suffix = " LIMIT ?"
            values.append(limit)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM levels" + where + " ORDER BY symbol, timeframe, confirmed_at_ms, level_id" + suffix,
                values,
            ).fetchall()
        return [_level_mapping(row) for row in rows]

    def level_history_rows(self, level_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Return archived level snapshots, newest archive first."""
        if limit < 1:
            return []
        values: list[Any] = [level_id] if level_id is not None else []
        where = " WHERE level_id=?" if level_id is not None else ""
        values.append(limit)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM level_history" + where + " ORDER BY archived_at_ms DESC, history_id DESC LIMIT ?",
                values,
            ).fetchall()
        result = []
        for row in rows:
            item = _level_mapping(row)
            item["history_id"] = row["history_id"]
            item["archived_at_ms"] = row["archived_at_ms"]
            result.append(item)
        return result

    def mark_level_broken(
        self,
        level_id: str,
        broken_at_ms: int,
        invalidation_reason: str | None = None,
        *,
        updated_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Record a break without allowing a later snapshot to revive a level."""
        current = self.level_row(level_id)
        if current is None:
            raise ValueError(f"unknown level: {level_id}")
        updated = dict(current)
        updated["broken_at_ms"] = broken_at_ms
        if invalidation_reason is not None:
            updated["invalidation_reason"] = invalidation_reason
        updated["updated_at_ms"] = broken_at_ms if updated_at_ms is None else updated_at_ms
        return self.upsert_level(updated)

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
            meta = db.execute("SELECT key,value FROM meta").fetchall()
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
            "meta": {str(row["key"]): str(row["value"]) for row in meta},
        }

    def position_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM positions WHERE status='CLOSED' ORDER BY closed_at_ms DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def attempted_setup_ids(self) -> set[str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT features_json FROM signals WHERE status IN ('TRIGGERED','OPEN','CLOSED','MISSED')"
            ).fetchall()
        result: set[str] = set()
        for row in rows:
            setup_id = json.loads(row["features_json"]).get("setup_id")
            if setup_id:
                result.add(str(setup_id))
        return result

    def reconcile_orphaned_triggers(self, reason: str = "restart_before_paper_entry") -> int:
        """Mark memory-only pending attempts as missed after a restart."""
        with self.connect() as db:
            cursor = db.execute(
                """UPDATE signals SET status='MISSED', reason=?
                   WHERE status='TRIGGERED'
                     AND signal_id NOT IN (SELECT signal_id FROM positions)
                     AND signal_id NOT IN (
                         SELECT signal_id FROM paper_orders
                         WHERE status IN ('PENDING', 'ACTIVE', 'PARTIAL')
                     )""",
                (reason,),
            )
        return cursor.rowcount

    def paper_order_row(self, signal_id: str | None = None) -> dict[str, Any] | None:
        with self.connect() as db:
            if signal_id is None:
                row = db.execute(
                    """SELECT * FROM paper_orders
                       WHERE status IN ('PENDING', 'ACTIVE', 'PARTIAL')
                       ORDER BY created_at_ms DESC
                       LIMIT 1"""
                ).fetchone()
            else:
                row = db.execute("SELECT * FROM paper_orders WHERE signal_id=?", (signal_id,)).fetchone()
        return dict(row) if row else None

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


# Keep the SQL column order in one place so inserts, history snapshots, and
# updates cannot silently drift apart as the catalog evolves.
_LEVEL_STORAGE_FIELDS = (
    "level_id",
    "revision",
    "version",
    "symbol",
    "timeframe",
    "side",
    "price",
    "zone_low",
    "zone_high",
    "touches",
    "level_class",
    "provenance_json",
    "origin_at_ms",
    "confirmed_at_ms",
    "first_seen_at_ms",
    "broken_at_ms",
    "invalidation_reason",
    "updated_at_ms",
)


_SIGNAL_PATH_FIELDS = (
    "event_id",
    "signal_id",
    "occurred_at_ms",
    "event_type",
    "price",
    "reference_price",
    "tp_touched",
    "sl_touched",
    "mfe_bp",
    "mae_bp",
    "coverage",
    "status",
    "reason",
    "features_json",
    "provenance_json",
)


def _normalise_level(level: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a caller mapping or level-like object to storage columns."""
    if "level_id" not in level:
        raise KeyError("level_id")
    level_id = str(level["level_id"])
    if not level_id:
        raise ValueError("level_id is required")

    price_value = level.get("price", level.get("representative_price"))
    if price_value is None:
        raise KeyError("price")
    price = float(price_value)
    zone_low = float(level.get("zone_low", price))
    zone_high = float(level.get("zone_high", price))
    if zone_low > zone_high:
        raise ValueError("zone_low cannot exceed zone_high")

    side_value = level.get("side")
    side = getattr(side_value, "value", side_value)
    side = str(side).upper() if side is not None else ""
    if side not in {"HIGH", "LOW"}:
        raise ValueError("side must be HIGH or LOW")

    confirmed_value = level.get("confirmed_at_ms")
    if confirmed_value is None:
        raise KeyError("confirmed_at_ms")
    confirmed_at_ms = int(confirmed_value)
    origin_value = level.get("origin_at_ms")
    origin_at_ms = int(confirmed_at_ms if origin_value is None else origin_value)
    first_seen_value = level.get("first_seen_at_ms")
    first_seen_at_ms = int(confirmed_at_ms if first_seen_value is None else first_seen_value)
    if origin_at_ms > confirmed_at_ms:
        raise ValueError("origin_at_ms cannot exceed confirmed_at_ms")
    if first_seen_at_ms < confirmed_at_ms:
        raise ValueError("first_seen_at_ms cannot precede confirmed_at_ms")

    broken_value = level.get("broken_at_ms")
    broken_at_ms = int(broken_value) if broken_value is not None else None
    if broken_at_ms is not None and broken_at_ms < confirmed_at_ms:
        raise ValueError("broken_at_ms cannot precede confirmed_at_ms")

    provenance = level.get("provenance")
    if provenance is None and level.get("provenance_json") is not None:
        raw_provenance = level["provenance_json"]
        provenance = json.loads(raw_provenance) if isinstance(raw_provenance, str) else raw_provenance
    if provenance is None:
        provenance = {}
    updated_at_ms = int(level.get("updated_at_ms", max(confirmed_at_ms, first_seen_at_ms, broken_at_ms or 0)))
    return {
        "level_id": level_id,
        "revision": int(level.get("revision", 1)),
        "version": str(level.get("version", level.get("level_version", "v1"))),
        "symbol": str(level["symbol"]),
        "timeframe": str(level["timeframe"]),
        "side": side,
        "price": price,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "touches": int(level.get("touches", 1)),
        "level_class": str(level.get("level_class", level.get("class", "swing"))),
        "provenance_json": _json(provenance),
        "origin_at_ms": origin_at_ms,
        "confirmed_at_ms": confirmed_at_ms,
        "first_seen_at_ms": first_seen_at_ms,
        "broken_at_ms": broken_at_ms,
        "invalidation_reason": level.get("invalidation_reason"),
        "updated_at_ms": updated_at_ms,
    }


def _merge_level(existing: sqlite3.Row, incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a new snapshot while retaining monotonic lifecycle state."""
    current = dict(existing)
    # A stale detector/bootstrap result must not roll back a newer revision.
    if int(incoming["revision"]) >= int(current["revision"]):
        merged = dict(incoming)
    else:
        merged = dict(current)

    # first_seen is a fact about the formation and therefore only moves back
    # when an older constituent becomes visible; it must never move forward.
    merged["origin_at_ms"] = min(int(current["origin_at_ms"]), int(incoming["origin_at_ms"]))
    merged["confirmed_at_ms"] = min(int(current["confirmed_at_ms"]), int(incoming["confirmed_at_ms"]))
    merged["first_seen_at_ms"] = min(int(current["first_seen_at_ms"]), int(incoming["first_seen_at_ms"]))
    merged["updated_at_ms"] = max(int(current["updated_at_ms"]), int(incoming["updated_at_ms"]))

    old_break = current.get("broken_at_ms")
    new_break = incoming.get("broken_at_ms")
    breaks = [int(value) for value in (old_break, new_break) if value is not None]
    if breaks:
        merged["broken_at_ms"] = min(breaks)
        if old_break is not None and current.get("invalidation_reason"):
            merged["invalidation_reason"] = current["invalidation_reason"]
        elif incoming.get("invalidation_reason"):
            merged["invalidation_reason"] = incoming["invalidation_reason"]
    else:
        merged["broken_at_ms"] = None
        merged["invalidation_reason"] = incoming.get("invalidation_reason")
    return merged


def _level_mapping(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    provenance_json = item.pop("provenance_json", "{}")
    item["provenance"] = json.loads(provenance_json)
    return item


def _normalise_signal_path_event(event: Mapping[str, Any]) -> dict[str, Any]:
    signal_id = str(event.get("signal_id", ""))
    if not signal_id:
        raise KeyError("signal_id")
    timestamp = event.get("occurred_at_ms", event.get("event_at_ms", event.get("timestamp_ms")))
    if timestamp is None:
        raise KeyError("occurred_at_ms")
    occurred_at_ms = int(timestamp)
    event_type = str(event.get("event_type", event.get("type", "")))
    if not event_type:
        raise KeyError("event_type")
    event_id = str(event.get("event_id") or f"{signal_id}:{occurred_at_ms}:{event_type}")

    def optional_number(name: str) -> float | None:
        value = event.get(name)
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be numeric or null") from exc
        if not math.isfinite(number):
            raise ValueError(f"{name} must be finite or null")
        return number

    coverage = optional_number("coverage")
    if coverage is not None and not 0 <= coverage <= 1:
        raise ValueError("coverage must be between 0 and 1")
    features = event.get("features")
    if features is None and event.get("features_json") is not None:
        raw_features = event["features_json"]
        features = json.loads(raw_features) if isinstance(raw_features, str) else raw_features
    provenance = event.get("provenance")
    if provenance is None and event.get("provenance_json") is not None:
        raw_provenance = event["provenance_json"]
        provenance = json.loads(raw_provenance) if isinstance(raw_provenance, str) else raw_provenance

    return {
        "event_id": event_id,
        "signal_id": signal_id,
        "occurred_at_ms": occurred_at_ms,
        "event_type": event_type,
        "price": optional_number("price"),
        "reference_price": optional_number("reference_price"),
        "tp_touched": _touch_state(event.get("tp_touched", event.get("tp_touch"))),
        "sl_touched": _touch_state(event.get("sl_touched", event.get("sl_touch"))),
        "mfe_bp": optional_number("mfe_bp"),
        "mae_bp": optional_number("mae_bp"),
        "coverage": coverage,
        "status": str(event.get("status", "UNKNOWN")),
        "reason": str(event.get("reason", "")),
        "features_json": _json(features if features is not None else {}),
        "provenance_json": _json(provenance if provenance is not None else {}),
    }


def _touch_state(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    if isinstance(value, bool):
        return "YES" if value else "NO"
    normalised = str(value).upper().strip().replace("-", "_")
    aliases = {
        "TRUE": "YES",
        "FALSE": "NO",
        "TOUCHED": "YES",
        "NOT_TOUCHED": "NO",
        "NOT TOUCHED": "NO",
    }
    normalised = aliases.get(normalised, normalised)
    if normalised not in {"YES", "NO", "AMBIGUOUS", "UNKNOWN"}:
        raise ValueError("touch state must be YES, NO, AMBIGUOUS, or UNKNOWN")
    return normalised


def _signal_path_mapping(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["features"] = json.loads(item.pop("features_json"))
    item["provenance"] = json.loads(item.pop("provenance_json"))
    return item
