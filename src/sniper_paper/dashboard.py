"""Read-only dashboard helpers for the Sniper Paper project.

The dashboard is intentionally data-source agnostic. A caller provides a
callback that returns either a :class:`DashboardSnapshot` or a plain mapping
with compatible fields. The module then renders a single HTML page suitable for
operator review without exposing any write controls.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, Protocol

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
TEMPLATE_PATH = STATIC_DIR / "dashboard.html"


class DashboardProvider(Protocol):
    """Callback that supplies dashboard state for rendering."""

    def __call__(self) -> DashboardSnapshot | Mapping[str, Any] | None:
        raise NotImplementedError


@dataclass(slots=True)
class HealthItem:
    """Small status chip used for service and data health."""

    label: str
    status: str
    detail: str = ""


@dataclass(slots=True)
class UniverseItem:
    """Daily universe entry selected for the current UTC day."""

    symbol: str
    side: str = ""
    reason: str = ""
    timeframe: str = ""
    spread_bp: str = ""
    depth: str = ""


@dataclass(slots=True)
class PositionItem:
    """Paper position summary for the default screen."""

    symbol: str
    side: str
    entry: str = ""
    stop: str = ""
    target: str = ""
    mfe: str = ""
    mae: str = ""
    pnl: str = ""
    opened_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class SignalItem:
    """Recent signal or non-trade outcome."""

    timestamp: str
    symbol: str
    lane: str
    outcome: str
    detail: str = ""


@dataclass(slots=True)
class LanePnLItem:
    """Lane-specific simulated P&L summary."""

    lane: str
    pnl: str
    trades: str = ""
    wins: str = ""
    losses: str = ""


@dataclass(slots=True)
class DashboardSnapshot:
    """Complete snapshot rendered by the dashboard page."""

    generated_at: str
    last_update: str = ""
    service_health: Sequence[HealthItem] = field(default_factory=tuple)
    data_health: Sequence[HealthItem] = field(default_factory=tuple)
    current_universe: Sequence[UniverseItem] = field(default_factory=tuple)
    active_positions: Sequence[PositionItem] = field(default_factory=tuple)
    recent_signals: Sequence[SignalItem] = field(default_factory=tuple)
    lane_pnl: Sequence[LanePnLItem] = field(default_factory=tuple)
    headline: str = ""

    @classmethod
    def empty(cls, *, generated_at: str | None = None) -> DashboardSnapshot:
        """Return a safe empty snapshot for the disconnected state."""

        stamp = generated_at or _utc_now()
        return cls(
            generated_at=stamp,
            last_update=stamp,
            service_health=(HealthItem("Service", "Waiting", "No provider connected"),),
            data_health=(HealthItem("Data", "Waiting", "No SQLite callback wired yet"),),
            current_universe=(),
            active_positions=(),
            recent_signals=(),
            lane_pnl=(),
            headline="No dashboard data connected yet.",
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> DashboardSnapshot:
        """Coerce a plain mapping into a typed snapshot."""

        def _items(values: Iterable[Mapping[str, Any]], item_type: type[Any]) -> tuple[Any, ...]:
            return tuple(item_type(**dict(value)) for value in values)

        generated_at = str(payload.get("generated_at", _utc_now()))
        service_health = _items(payload.get("service_health", ()), HealthItem)
        data_health = _items(payload.get("data_health", ()), HealthItem)
        current_universe = _items(payload.get("current_universe", ()), UniverseItem)
        active_positions = _items(payload.get("active_positions", ()), PositionItem)
        recent_signals = _items(payload.get("recent_signals", ()), SignalItem)
        lane_pnl = _items(payload.get("lane_pnl", ()), LanePnLItem)

        return cls(
            generated_at=generated_at,
            last_update=str(payload.get("last_update", generated_at)),
            service_health=service_health,
            data_health=data_health,
            current_universe=current_universe,
            active_positions=active_positions,
            recent_signals=recent_signals,
            lane_pnl=lane_pnl,
            headline=str(payload.get("headline", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the snapshot for the HTML template."""

        return {
            "generated_at": self.generated_at,
            "last_update": self.last_update,
            "headline": self.headline,
            "service_health": [asdict(item) for item in self.service_health],
            "data_health": [asdict(item) for item in self.data_health],
            "current_universe": [asdict(item) for item in self.current_universe],
            "active_positions": [asdict(item) for item in self.active_positions],
            "recent_signals": [asdict(item) for item in self.recent_signals],
            "lane_pnl": [asdict(item) for item in self.lane_pnl],
        }


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _json_for_script(payload: Mapping[str, Any] | DashboardSnapshot) -> str:
    if isinstance(payload, DashboardSnapshot):
        payload = payload.to_dict()
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def load_dashboard_snapshot(provider: DashboardProvider | None = None) -> DashboardSnapshot:
    """Load a snapshot from the provider or return an empty state."""

    if provider is None:
        return DashboardSnapshot.empty()
    snapshot = provider()
    if snapshot is None:
        return DashboardSnapshot.empty()
    if isinstance(snapshot, DashboardSnapshot):
        return snapshot
    return DashboardSnapshot.from_mapping(snapshot)


def render_dashboard_html(
    snapshot: DashboardSnapshot | Mapping[str, Any] | None = None,
    *,
    provider: DashboardProvider | None = None,
    template_path: Path | None = None,
) -> str:
    """Render the dashboard page as HTML.

    The result is read-only and suitable for a future web route or static export.
    """

    if snapshot is None:
        snapshot = load_dashboard_snapshot(provider)
    elif isinstance(snapshot, Mapping):
        snapshot = DashboardSnapshot.from_mapping(snapshot)

    template = (template_path or TEMPLATE_PATH).read_text(encoding="utf-8")
    return (
        template.replace("__DASHBOARD_TITLE__", "Sniper Paper Dashboard")
        .replace("__DASHBOARD_PAYLOAD__", _json_for_script(snapshot))
        .replace("__DASHBOARD_GENERATED__", escape(snapshot.generated_at))
    )


def render_dashboard_payload(snapshot: DashboardSnapshot | Mapping[str, Any] | None = None) -> str:
    """Return the JSON payload that the template embeds."""

    if snapshot is None:
        snapshot = DashboardSnapshot.empty()
    elif isinstance(snapshot, Mapping):
        snapshot = DashboardSnapshot.from_mapping(snapshot)
    return _json_for_script(snapshot)
