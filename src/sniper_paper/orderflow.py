"""Causal footprint and DOM evidence for public Bybit feed rows."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from .market import FeedClock, FeedReadiness


def _decimal(value: Any, what: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"boolean is not a valid {what}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid {what}") from exc
    if not result.is_finite():
        raise ValueError(f"{what} must be finite")
    return result


def _duration_ms(value_ms: int | None, value_ns: int | None, *, default_ms: int, what: str) -> int:
    if value_ms is not None and value_ns is not None and value_ms != value_ns // 1_000_000:
        raise ValueError(f"conflicting {what} values")
    if value_ms is None and value_ns is None:
        return default_ms
    result = value_ms if value_ms is not None else value_ns // 1_000_000
    if result is None or result <= 0:
        raise ValueError(f"{what} must be positive")
    return int(result)


def _received_ms(row: dict[str, Any]) -> int:
    received = row.get("received_at_ms")
    if isinstance(received, int):
        return received
    received = row.get("received_ns")
    if isinstance(received, int):
        return received // 1_000_000
    raise ValueError("row.received_at_ms or row.received_ns must be an integer")


def _message(row: dict[str, Any]) -> dict[str, Any]:
    message = row.get("message", row)
    if not isinstance(message, dict):
        raise TypeError("message is required")
    return message


class Footprint:
    """Causal 15 second footprint buckets with reconnect quarantine."""

    def __init__(
        self,
        tick_sizes: dict[str, str],
        *,
        interval_ms: int | None = None,
        interval_ns: int | None = None,
        imbalance_ratio: int | str = 3,
        min_qty: str = "0",
        stack_levels: int = 3,
        dedup_window: int = 100_000,
        warmup_ms: int | None = None,
        warmup_ns: int | None = None,
        gap_ms: int | None = None,
        gap_ns: int | None = None,
    ) -> None:
        self.interval_ms = _duration_ms(interval_ms, interval_ns, default_ms=15_000, what="interval")
        self.warmup_ms = _duration_ms(warmup_ms, warmup_ns, default_ms=300_000, what="warmup")
        self.gap_ms = _duration_ms(gap_ms, gap_ns, default_ms=5_000, what="gap")
        if stack_levels < 1 or dedup_window < 1:
            raise ValueError("stack_levels and dedup_window must be positive")
        self.ticks = {str(symbol): _decimal(value, "tick size") for symbol, value in tick_sizes.items()}
        if not self.ticks or any(tick <= 0 or not tick.is_finite() for tick in self.ticks.values()):
            raise ValueError("tick sizes must be finite and positive")
        self.ratio = _decimal(imbalance_ratio, "imbalance ratio")
        self.min_qty = _decimal(min_qty, "min_qty")
        if self.ratio < 0 or self.min_qty < 0:
            raise ValueError("imbalance_ratio and min_qty must be non-negative")
        self.stack_levels = stack_levels
        self.dedup_window = dedup_window
        self._active: dict[tuple[str, int], dict[str, Any]] = {}
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._last_received_ms: int | None = None
        self._connection_id: str | None = None
        self._started_symbols: set[str] = set()
        self._invalid_until_ms: dict[str, int] = {}
        self._warmup_reason: dict[str, str] = {}
        self._feed = FeedClock(gap_ms=self.gap_ms, warmup_ms=self.warmup_ms)

    @staticmethod
    def _s(value: Decimal | float) -> str:
        return format(Decimal(str(value)), "f")

    def _mark(self, bucket: dict[str, Any], reason: str) -> None:
        bucket["incomplete"] = True
        if reason not in bucket["incomplete_reasons"]:
            bucket["incomplete_reasons"].append(reason)

    def _new_bucket(self, symbol: str, start_ms: int, readiness: FeedReadiness) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "bucket_start_ms": start_ms,
            "bucket_end_ms": start_ms + self.interval_ms,
            "bucket_start_ns": start_ms * 1_000_000,
            "bucket_end_ns": (start_ms + self.interval_ms) * 1_000_000,
            "partial": False,
            "incomplete": False,
            "incomplete_reasons": [],
            "levels": {},
            "feed_readiness": asdict(readiness),
            "connection_id": readiness.connection_id,
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "range": None,
            "volume": self._s(Decimal(0)),
            "delta": self._s(Decimal(0)),
            "delta_notional": self._s(Decimal(0)),
            "trades": 0,
        }

    def _flush_before(self, start_ms: int, out: list[dict[str, Any]]) -> None:
        keys = sorted([key for key in self._active if key[1] < start_ms], key=lambda item: item[1])
        for key in keys:
            out.append(self._render(self._active.pop(key)))

    def process(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        received = _received_ms(row)
        if self._last_received_ms is not None and received < self._last_received_ms:
            raise ValueError("received time is not monotonic")
        connection_id = row.get("connection_id")
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_id is required")
        message = _message(row)
        topic = message.get("topic", "")
        if not isinstance(topic, str):
            topic = str(topic)
        snapshot_seen = bool(
            topic.startswith("orderbook.50.")
            and (
                message.get("type") == "snapshot"
                or (isinstance(message.get("data"), dict) and message["data"].get("u") == 1)
            )
        )
        readiness = self._feed.observe(connection_id, received, snapshot=snapshot_seen)
        reconnect = self._connection_id is not None and connection_id != self._connection_id
        reconnect = reconnect or readiness.quarantine_reason == "reconnect_warmup"
        gap = self._last_received_ms is not None and received - self._last_received_ms > self.gap_ms
        current_ms = (received // self.interval_ms) * self.interval_ms
        out: list[dict[str, Any]] = []
        if reconnect or gap:
            for bucket in self._active.values():
                if reconnect:
                    self._mark(bucket, "reconnect")
                if gap:
                    self._mark(bucket, "received_gap_gt_5s")
            reason = "reconnect_warmup" if reconnect else "received_gap_warmup"
            for symbol in self.ticks:
                self._invalid_until_ms[symbol] = received + self.warmup_ms
                self._warmup_reason[symbol] = reason
        self._flush_before(current_ms, out)
        self._last_received_ms = received
        self._connection_id = connection_id
        if snapshot_seen:
            symbol = topic.rsplit(".", 1)[-1]
            if symbol in self.ticks:
                self._invalid_until_ms[symbol] = received + self.warmup_ms
                self._warmup_reason[symbol] = "snapshot_warmup"
        if not topic.startswith("publicTrade."):
            return out
        symbol = topic[len("publicTrade.") :]
        if symbol not in self.ticks:
            raise ValueError(f"missing tick size for {symbol}")
        data = message.get("data")
        if not isinstance(data, list):
            raise TypeError("publicTrade data must be a list")
        bucket = self._active.setdefault((symbol, current_ms), self._new_bucket(symbol, current_ms, readiness))
        bucket["feed_readiness"] = asdict(readiness)
        bucket["connection_id"] = connection_id
        if symbol not in self._started_symbols and received != current_ms:
            self._mark(bucket, "start_midbucket")
        if reconnect:
            self._mark(bucket, "reconnect")
        if gap:
            self._mark(bucket, "received_gap_gt_5s")
        if received < self._invalid_until_ms.get(symbol, 0):
            self._mark(bucket, self._warmup_reason[symbol])
        levels = bucket["levels"]
        for trade in data:
            if not isinstance(trade, dict) or not trade.get("i"):
                raise ValueError("trade id is mandatory")
            if trade.get("s") != symbol:
                raise ValueError("trade symbol does not match topic")
            dedup = (symbol, str(trade["i"]))
            if dedup in self._seen:
                self._seen.move_to_end(dedup)
                continue
            price = _decimal(trade.get("p"), "trade price")
            qty = _decimal(trade.get("v"), "trade quantity")
            tick = self.ticks[symbol]
            if price <= 0 or qty <= 0 or (price / tick) != (price / tick).to_integral_value():
                raise ValueError("trade price/quantity must be positive and price tick-aligned")
            side = trade.get("S")
            if side not in ("Buy", "Sell"):
                raise ValueError("trade side must be Buy or Sell")
            level = levels.setdefault(
                price,
                {
                    "buy_qty": Decimal(0),
                    "sell_qty": Decimal(0),
                    "buy_notional": Decimal(0),
                    "sell_notional": Decimal(0),
                },
            )
            prefix = "buy" if side == "Buy" else "sell"
            level[f"{prefix}_qty"] += qty
            level[f"{prefix}_notional"] += price * qty
            self._seen[dedup] = None
            self._seen.move_to_end(dedup)
            while len(self._seen) > self.dedup_window:
                self._seen.popitem(last=False)
            bucket["trades"] += 1
            bucket["volume"] = self._s(Decimal(bucket["volume"]) + qty)
            if bucket["open"] is None:
                bucket["open"] = self._s(price)
                bucket["high"] = self._s(price)
                bucket["low"] = self._s(price)
            else:
                bucket["high"] = self._s(max(Decimal(bucket["high"]), price))
                bucket["low"] = self._s(min(Decimal(bucket["low"]), price))
            bucket["close"] = self._s(price)
            bucket["delta"] = self._s(Decimal(bucket["delta"]) + (qty if side == "Buy" else -qty))
            bucket["delta_notional"] = self._s(
                Decimal(bucket["delta_notional"]) + ((price * qty) if side == "Buy" else -(price * qty))
            )
        self._started_symbols.add(symbol)
        return out

    def _qual(self, num: Decimal, den: Decimal) -> tuple[str | None, bool]:
        if den == 0 or num < self.min_qty:
            return None, False
        ratio = num / den
        return self._s(ratio), ratio >= self.ratio

    def _render(self, bucket: dict[str, Any]) -> dict[str, Any]:
        levels = bucket["levels"]
        prices = sorted(levels)
        rows = []
        imbalances = []
        for price in prices:
            level = levels[price]
            tick = self.ticks[bucket["symbol"]]
            same_buy, same_buy_ok = self._qual(level["buy_qty"], level["sell_qty"])
            same_sell, same_sell_ok = self._qual(level["sell_qty"], level["buy_qty"])
            below = levels.get(price - tick, {})
            above = levels.get(price + tick, {})
            diag_buy, diag_buy_ok = self._qual(level["buy_qty"], below.get("sell_qty", Decimal(0)))
            diag_sell, diag_sell_ok = self._qual(level["sell_qty"], above.get("buy_qty", Decimal(0)))
            total = level["buy_qty"] + level["sell_qty"]
            item = {
                "price": self._s(price),
                "buy_qty": self._s(level["buy_qty"]),
                "sell_qty": self._s(level["sell_qty"]),
                "buy_notional": self._s(level["buy_notional"]),
                "sell_notional": self._s(level["sell_notional"]),
                "delta": self._s(level["buy_qty"] - level["sell_qty"]),
                "total": self._s(total),
                "same_buy_sell_ratio": same_buy,
                "same_sell_buy_ratio": same_sell,
                "diagonal_buy_sell_ratio": diag_buy,
                "diagonal_sell_buy_ratio": diag_sell,
                "same_buy_imbalance": same_buy_ok,
                "same_sell_imbalance": same_sell_ok,
                "diagonal_buy_imbalance": diag_buy_ok,
                "diagonal_sell_imbalance": diag_sell_ok,
                "same_buy_one_sided": level["buy_qty"] > 0 and level["sell_qty"] == 0,
                "same_sell_one_sided": level["sell_qty"] > 0 and level["buy_qty"] == 0,
                "diagonal_buy_one_sided": level["buy_qty"] > 0 and below.get("sell_qty", Decimal(0)) == 0,
                "diagonal_sell_one_sided": level["sell_qty"] > 0 and above.get("buy_qty", Decimal(0)) == 0,
            }
            item.update(
                {
                    "diagnostic_same_buy_imbalance": item["same_buy_imbalance"],
                    "diagnostic_same_sell_imbalance": item["same_sell_imbalance"],
                    "diagnostic_diagonal_buy_imbalance": item["diagonal_buy_imbalance"],
                    "diagnostic_diagonal_sell_imbalance": item["diagonal_sell_imbalance"],
                }
            )
            if bucket["incomplete"]:
                item["same_buy_imbalance"] = False
                item["same_sell_imbalance"] = False
                item["diagonal_buy_imbalance"] = False
                item["diagonal_sell_imbalance"] = False
            rows.append(item)
            for kind, side, ratio, ok in (
                ("same", "buy", same_buy, same_buy_ok),
                ("same", "sell", same_sell, same_sell_ok),
                ("diagonal", "buy", diag_buy, diag_buy_ok),
                ("diagonal", "sell", diag_sell, diag_sell_ok),
            ):
                if ok:
                    imbalances.append({"kind": kind, "side": side, "price": self._s(price), "ratio": ratio})
        stacks = []
        for side in ("buy", "sell"):
            run: list[str] = []
            previous_price: Decimal | None = None
            for item in rows:
                item_price = Decimal(item["price"])
                ok = (
                    item["diagnostic_diagonal_buy_imbalance"]
                    if side == "buy"
                    else item["diagnostic_diagonal_sell_imbalance"]
                )
                if previous_price is not None and item_price - previous_price != self.ticks[bucket["symbol"]]:
                    if len(run) >= self.stack_levels:
                        stacks.append({"kind": "diagonal", "side": side, "prices": run})
                    run = []
                if ok:
                    run.append(item["price"])
                else:
                    if len(run) >= self.stack_levels:
                        stacks.append({"kind": "diagonal", "side": side, "prices": run})
                    run = []
                previous_price = item_price
            if len(run) >= self.stack_levels:
                stacks.append({"kind": "diagonal", "side": side, "prices": run})
        low = Decimal(bucket["low"]) if bucket["low"] is not None else None
        high = Decimal(bucket["high"]) if bucket["high"] is not None else None
        bucket["levels"] = rows
        bucket["range"] = None if low is None or high is None else self._s(high - low)
        bucket["diagnostic_imbalances"] = imbalances
        bucket["diagnostic_stacks"] = stacks
        bucket["imbalances"] = [] if bucket["incomplete"] else imbalances
        bucket["stacks"] = [] if bucket["incomplete"] else stacks
        return bucket

    def finish(self) -> list[dict[str, Any]]:
        out = []
        for key in sorted(self._active, key=lambda item: item[1]):
            bucket = self._active[key]
            bucket["partial"] = True
            out.append(self._render(bucket))
        self._active.clear()
        return out


FootprintTracker = Footprint


class DomTracker:
    """Causal, uncertainty-preserving depth-wall tracking for public streams."""

    def __init__(
        self,
        wall_min_notional: dict[str, str],
        *,
        wall_multiple: str = "3",
        persistence_ms: int | None = None,
        persistence_ns: int | None = None,
        max_age_ms: int | None = None,
        max_age_ns: int | None = None,
        refill_window_ms: int | None = None,
        refill_window_ns: int | None = None,
        visible_levels: int = 50,
        gap_ms: int | None = None,
        gap_ns: int | None = None,
        warmup_ms: int | None = None,
        warmup_ns: int | None = None,
    ) -> None:
        self.wall_min = {str(symbol): _decimal(value, "wall threshold") for symbol, value in wall_min_notional.items()}
        if not self.wall_min or any(value < 0 for value in self.wall_min.values()):
            raise ValueError("invalid DOM tracker configuration")
        self.multiple = _decimal(wall_multiple, "wall_multiple")
        self.persistence_ms = _duration_ms(persistence_ms, persistence_ns, default_ms=1_000, what="persistence")
        self.max_age_ms = _duration_ms(max_age_ms, max_age_ns, default_ms=500, what="max_age")
        self.refill_window_ms = _duration_ms(refill_window_ms, refill_window_ns, default_ms=1_000, what="refill_window")
        self.gap_ms = _duration_ms(gap_ms, gap_ns, default_ms=5_000, what="gap")
        self.warmup_ms = _duration_ms(warmup_ms, warmup_ns, default_ms=300_000, what="warmup")
        self.visible_levels = int(visible_levels)
        if (
            self.multiple <= 0
            or self.visible_levels < 1
            or min(self.persistence_ms, self.max_age_ms, self.refill_window_ms, self.gap_ms, self.warmup_ms) < 0
        ):
            raise ValueError("invalid DOM tracker configuration")
        self._states: dict[str, dict[str, Any]] = {}
        self._last_received_ms: int | None = None

    @staticmethod
    def _symbol(message: dict[str, Any]) -> str:
        topic = str(message.get("topic", ""))
        return topic.rsplit(".", 1)[-1]

    @staticmethod
    def _levels(items: Any) -> dict[Decimal, Decimal]:
        result: dict[Decimal, Decimal] = {}
        if not isinstance(items, list):
            raise TypeError("book side is not a list")
        for item in items:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                raise ValueError("malformed book level")
            price, quantity = _decimal(item[0], "book price"), _decimal(item[1], "book quantity")
            if price <= 0 or quantity < 0:
                raise ValueError("invalid book level")
            result[price] = quantity
        return result

    def _visible(self, book: dict[Decimal, Decimal], side: str) -> dict[Decimal, Decimal]:
        prices = sorted(book, reverse=side == "bid")[: self.visible_levels]
        return {price: book[price] for price in prices if book[price] > 0}

    def _reset_evidence(self, state: dict[str, Any]) -> None:
        state["first_candidate"] = {}
        state["persistent_emitted"] = set()
        state["reductions"] = {}
        state["trades"] = OrderedDict()

    def _threshold(self, symbol: str, state: dict[str, Any], side: str) -> Decimal:
        values = [price * qty for price, qty in state["book"][side].items() if qty > 0]
        med = _decimal(str(median(values)), "median") if values else Decimal(0)
        return max(self.wall_min.get(symbol, Decimal(0)), self.multiple * med)

    def _event(
        self,
        typ: str,
        symbol: str,
        side: str,
        price: Decimal,
        row: dict[str, Any],
        state: dict[str, Any],
        qty: Decimal,
        old_qty: Decimal | None = None,
        *,
        readiness: FeedReadiness,
    ) -> dict[str, Any]:
        received = _received_ms(row)
        notional = price * qty
        threshold = self._threshold(symbol, state, side)
        candidate = qty > 0 and notional >= threshold
        key = (side, price)
        previous_notional = price * old_qty if old_qty is not None else Decimal(0)
        previous_candidate = old_qty is not None and old_qty > 0 and previous_notional >= threshold
        first = state["first_candidate"].get(key)
        if candidate:
            if first is None:
                state["first_candidate"][key] = received
                first = received
        else:
            state["first_candidate"].pop(key, None)
        persistent = bool(candidate and first is not None and received - first >= self.persistence_ms)
        reduction = state["reductions"].get(key)
        refill = bool(typ == "depth_added" and reduction is not None and received - reduction <= self.refill_window_ms)
        trade_qty = state["trades"].get(key, Decimal(0))
        if typ == "level_removed":
            evidence_label = "removal"
            uncertainty = "ambiguous"
        elif typ == "depth_added" and refill:
            evidence_label = "refill"
            uncertainty = "inferred"
        elif typ == "depth_reduced":
            evidence_label = "reduction"
            uncertainty = "observed"
        else:
            evidence_label = "persistent_wall" if typ == "wall_persistent" else typ
            uncertainty = "observed"
        event = {
            "type": typ,
            "symbol": symbol,
            "side": side,
            "price": str(price),
            "received_ms": received,
            "received_ns": received * 1_000_000,
            "connection_id": row.get("connection_id"),
            "quantity": str(qty),
            "notional": str(notional),
            "wall_candidate": candidate,
            "wall_persistent": persistent,
            "previous_wall_candidate": previous_candidate,
            "refill_candidate": refill,
            "trade_evidence_qty": str(trade_qty),
            "uncertainty": uncertainty,
            "evidence_label": evidence_label,
            "feed_readiness": asdict(readiness),
        }
        if first is not None:
            event["first_candidate_at_ms"] = first
            event["wall_age_ms"] = received - first
        if typ == "level_removed":
            event["removal_attribution"] = "unknown_execution_or_cancel_or_visibility"
        if old_qty is not None:
            event["previous_quantity"] = str(old_qty)
        state["trades"].pop(key, None)
        return event

    def _seed_candidates(self, symbol: str, state: dict[str, Any], received: int) -> None:
        for side in ("bid", "ask"):
            threshold = self._threshold(symbol, state, side)
            for price, qty in state["book"][side].items():
                if qty > 0 and price * qty >= threshold:
                    state["first_candidate"][(side, price)] = received

    def _persistent_events(
        self, symbol: str, row: dict[str, Any], state: dict[str, Any], readiness: FeedReadiness
    ) -> list[dict[str, Any]]:
        received = _received_ms(row)
        live: set[tuple[str, Decimal]] = set()
        events = []
        for side in ("bid", "ask"):
            threshold = self._threshold(symbol, state, side)
            for price, qty in state["book"][side].items():
                key = (side, price)
                if qty <= 0 or price * qty < threshold:
                    continue
                live.add(key)
                first = state["first_candidate"].setdefault(key, received)
                if received - first >= self.persistence_ms and key not in state["persistent_emitted"]:
                    event = self._event(
                        "wall_persistent", symbol, side, price, row, state, qty, qty, readiness=readiness
                    )
                    event["wall_persistent"] = True
                    event["uncertainty"] = "observed"
                    events.append(event)
                    state["persistent_emitted"].add(key)
        for key in set(state["first_candidate"]) - live:
            state["first_candidate"].pop(key, None)
            state["persistent_emitted"].discard(key)
        return events

    def _trade(self, symbol: str, data: Any, state: dict[str, Any]) -> None:
        if not isinstance(data, list):
            raise TypeError("publicTrade data must be a list")
        for trade in data:
            if not isinstance(trade, dict) or not trade.get("i"):
                raise ValueError("trade id is mandatory")
            price, qty = _decimal(trade.get("p"), "trade price"), _decimal(trade.get("v"), "trade quantity")
            if price <= 0 or qty <= 0 or trade.get("s") not in (None, symbol):
                raise ValueError("invalid public trade")
            if trade.get("S") not in ("Buy", "Sell"):
                raise ValueError("invalid public trade side")
            side = "ask" if trade["S"] == "Buy" else "bid"
            dedup = (symbol, str(trade["i"]))
            if dedup in state["trade_ids"]:
                state["trade_ids"].move_to_end(dedup)
                continue
            state["trade_ids"][dedup] = None
            if len(state["trade_ids"]) > 20_000:
                state["trade_ids"].popitem(last=False)
            key = (side, price)
            state["trades"][key] = state["trades"].get(key, Decimal(0)) + qty
            state["trades"].move_to_end(key)
            while len(state["trades"]) > 20_000:
                state["trades"].popitem(last=False)

    def process(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        received = _received_ms(row)
        if self._last_received_ms is not None and received < self._last_received_ms:
            raise ValueError("received time is not monotonic")
        message = _message(row)
        topic = message.get("topic", "")
        if not isinstance(topic, str):
            topic = str(topic)
        if not topic.startswith(("publicTrade.", "orderbook.50.")):
            self._last_received_ms = received
            return []
        symbol = self._symbol(message)
        if symbol not in self.wall_min:
            raise ValueError(f"missing wall threshold for {symbol}")
        connection_id = row.get("connection_id", message.get("connection_id"))
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_id is required")
        state = self._states.setdefault(
            symbol,
            {
                "connection": connection_id,
                "valid": False,
                "book": {"bid": {}, "ask": {}},
                "last_u": None,
                "last_received": None,
                "first_candidate": {},
                "persistent_emitted": set(),
                "reductions": {},
                "trades": OrderedDict(),
                "trade_ids": OrderedDict(),
                "feed": FeedClock(gap_ms=self.gap_ms, warmup_ms=self.warmup_ms),
            },
        )
        snapshot_seen = bool(
            topic.startswith("orderbook.50.")
            and (
                message.get("type") == "snapshot"
                or (isinstance(message.get("data"), dict) and message["data"].get("u") == 1)
            )
        )
        data = message.get("data", {})
        readiness = state["feed"].observe(connection_id, received, snapshot=snapshot_seen)
        reconnect = state["connection"] != connection_id or readiness.quarantine_reason == "reconnect_warmup"
        if reconnect:
            state.update(
                connection=connection_id,
                valid=False,
                last_u=None,
                last_received=None,
                book={"bid": {}, "ask": {}},
                trades=OrderedDict(),
            )
            self._reset_evidence(state)
        if topic.startswith("publicTrade."):
            self._trade(symbol, data, state)
            self._last_received_ms = received
            return []
        if not isinstance(data, dict):
            self._last_received_ms = received
            return []
        try:
            u = int(data["u"])
        except (KeyError, TypeError, ValueError):
            state["valid"] = False
            self._last_received_ms = received
            return []
        if data.get("s") not in (None, symbol):
            state["valid"] = False
            self._reset_evidence(state)
            self._last_received_ms = received
            return []
        if state["last_received"] is not None and received - state["last_received"] > self.max_age_ms:
            self._reset_evidence(state)
        if message.get("type") == "snapshot" or u == 1:
            try:
                state["book"] = {
                    "bid": self._levels(data.get("b", [])),
                    "ask": self._levels(data.get("a", [])),
                }
            except ValueError:
                state["valid"] = False
                self._last_received_ms = received
                return []
            state["book"] = {side: self._visible(state["book"][side], side) for side in ("bid", "ask")}
            best_bid = max(state["book"]["bid"], default=None)
            best_ask = min(state["book"]["ask"], default=None)
            if best_bid is None or best_ask is None or best_bid >= best_ask:
                state["valid"] = False
                self._reset_evidence(state)
                self._last_received_ms = received
                return []
            state.update(valid=True, last_u=u, last_received=received)
            self._reset_evidence(state)
            self._seed_candidates(symbol, state, received)
            self._last_received_ms = received
            return []
        if state["valid"] is False or u <= 1 or state["last_u"] is None or u <= state["last_u"]:
            state["valid"] = False
            self._reset_evidence(state)
            self._last_received_ms = received
            return []
        old = {side: dict(state["book"][side]) for side in ("bid", "ask")}
        try:
            updates = {"bid": self._levels(data.get("b", [])), "ask": self._levels(data.get("a", []))}
        except ValueError:
            state["valid"] = False
            self._last_received_ms = received
            return []
        for side in ("bid", "ask"):
            for price, qty in updates[side].items():
                if qty == 0:
                    state["book"][side].pop(price, None)
                else:
                    state["book"][side][price] = qty
            state["book"][side] = self._visible(state["book"][side], side)
        best_bid = max(state["book"]["bid"], default=None)
        best_ask = min(state["book"]["ask"], default=None)
        if best_bid is not None and best_ask is not None and best_bid >= best_ask:
            state["valid"] = False
            self._reset_evidence(state)
            self._last_received_ms = received
            return []
        state.update(last_u=u, last_received=received)
        events: list[dict[str, Any]] = []
        for side in ("bid", "ask"):
            for price, qty in updates[side].items():
                previous = old[side].get(price)
                if qty == 0 and previous is not None:
                    state["reductions"][(side, price)] = received
                    events.append(
                        self._event(
                            "level_removed",
                            symbol,
                            side,
                            price,
                            row,
                            state,
                            Decimal(0),
                            previous,
                            readiness=readiness,
                        )
                    )
                elif previous is not None and qty > previous:
                    events.append(
                        self._event(
                            "depth_added",
                            symbol,
                            side,
                            price,
                            row,
                            state,
                            qty,
                            previous,
                            readiness=readiness,
                        )
                    )
                elif previous is not None and qty < previous:
                    state["reductions"][(side, price)] = received
                    events.append(
                        self._event(
                            "depth_reduced",
                            symbol,
                            side,
                            price,
                            row,
                            state,
                            qty,
                            previous,
                            readiness=readiness,
                        )
                    )
                elif previous is None and qty > 0:
                    old_prices = old[side]
                    inside_visible_range = bool(old_prices) and (
                        price >= min(old_prices) if side == "bid" else price <= max(old_prices)
                    )
                    if (price in state["book"][side]) and (
                        (side, price) in state["reductions"] or inside_visible_range
                    ):
                        events.append(
                            self._event(
                                "depth_added",
                                symbol,
                                side,
                                price,
                                row,
                                state,
                                qty,
                                Decimal(0),
                                readiness=readiness,
                            )
                        )
        events.extend(self._persistent_events(symbol, row, state, readiness))
        self._last_received_ms = received
        return events
