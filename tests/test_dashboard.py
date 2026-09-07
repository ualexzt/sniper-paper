from __future__ import annotations

import unittest
from pathlib import Path

from sniper_paper.dashboard import (
    DashboardSnapshot,
    HealthItem,
    LanePnLItem,
    PositionItem,
    SignalItem,
    UniverseItem,
    load_dashboard_snapshot,
    render_dashboard_html,
    render_dashboard_payload,
)


class DashboardTests(unittest.TestCase):
    def test_empty_snapshot_has_safe_defaults(self) -> None:
        snapshot = DashboardSnapshot.empty(generated_at="2026-09-07 12:00:00Z")

        self.assertEqual(snapshot.generated_at, "2026-09-07 12:00:00Z")
        self.assertEqual(snapshot.last_update, "2026-09-07 12:00:00Z")
        self.assertEqual(snapshot.headline, "No dashboard data connected yet.")
        self.assertEqual(snapshot.service_health[0].status, "Waiting")
        self.assertEqual(snapshot.data_health[0].detail, "No SQLite callback wired yet")

    def test_mapping_is_coerced_into_typed_snapshot(self) -> None:
        snapshot = DashboardSnapshot.from_mapping(
            {
                "generated_at": "2026-09-07 10:00:00Z",
                "last_update": "2026-09-07 10:01:00Z",
                "headline": "Ready",
                "service_health": [{"label": "API", "status": "Healthy", "detail": "up"}],
                "data_health": [{"label": "SQLite", "status": "Fresh", "detail": "synced"}],
                "current_universe": [{"symbol": "BTCUSDT", "side": "Long", "reason": "spread gate"}],
                "active_positions": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "Long",
                        "entry": "64500",
                        "stop": "64200",
                        "target": "65100",
                        "mfe": "+120",
                        "mae": "-30",
                        "pnl": "+85",
                        "opened_at": "10:00",
                        "updated_at": "10:01",
                    }
                ],
                "recent_signals": [
                    {
                        "timestamp": "10:01",
                        "symbol": "ETHUSDT",
                        "lane": "early_target_hunt",
                        "outcome": "MISSED",
                        "detail": "entry never crossed",
                    }
                ],
                "lane_pnl": [{"lane": "early_target_hunt", "pnl": "+1.20", "trades": "4", "wins": "3", "losses": "1"}],
            }
        )

        self.assertEqual(snapshot.service_health[0], HealthItem("API", "Healthy", "up"))
        self.assertEqual(snapshot.current_universe[0], UniverseItem("BTCUSDT", "Long", "spread gate", "", "", ""))
        self.assertEqual(snapshot.active_positions[0].target, "65100")
        self.assertEqual(snapshot.recent_signals[0].outcome, "MISSED")
        self.assertEqual(snapshot.lane_pnl[0].pnl, "+1.20")

    def test_load_dashboard_snapshot_uses_provider(self) -> None:
        def provider() -> dict[str, object]:
            return {
                "generated_at": "2026-09-07 11:00:00Z",
                "headline": "Connected",
                "service_health": [{"label": "Service", "status": "Healthy"}],
                "data_health": [{"label": "Data", "status": "Fresh"}],
            }

        snapshot = load_dashboard_snapshot(provider)

        self.assertEqual(snapshot.generated_at, "2026-09-07 11:00:00Z")
        self.assertEqual(snapshot.headline, "Connected")
        self.assertEqual(snapshot.service_health[0].status, "Healthy")

    def test_render_dashboard_html_includes_sections_and_escapes_content(self) -> None:
        snapshot = DashboardSnapshot(
            generated_at="2026-09-07 12:00:00Z",
            last_update="2026-09-07 12:10:00Z",
            headline="Snapshot <ready>",
            service_health=(HealthItem("Service", "Healthy", "ok"),),
            data_health=(HealthItem("SQLite", "Fresh", "synced"),),
            current_universe=(UniverseItem("BTCUSDT", "Long", "quote gate"),),
            active_positions=(
                PositionItem("BTCUSDT", "Long", "64500", "64200", "65100", "+120", "-30", "+85", "10:00", "10:10"),
            ),
            recent_signals=(SignalItem("10:05", "ETHUSDT", "terminal_level_breakout", "REJECTED", "filter < 1"),),
            lane_pnl=(LanePnLItem("terminal_level_breakout", "+2.50", "5", "4", "1"),),
        )

        html = render_dashboard_html(snapshot=snapshot, template_path=Path("src/sniper_paper/static/dashboard.html"))

        self.assertIn("Sniper Paper Dashboard", html)
        self.assertIn("Current daily universe", html)
        self.assertIn("Active paper positions", html)
        self.assertIn("Recent signals", html)
        self.assertIn("Lane P&amp;L", html)
        self.assertIn("Snapshot \\u003cready\\u003e", html)
        self.assertIn('"outcome":"REJECTED"', html)

    def test_render_dashboard_payload_matches_template_data_shape(self) -> None:
        payload = render_dashboard_payload(DashboardSnapshot.empty(generated_at="2026-09-07 12:00:00Z"))

        self.assertIn('"service_health"', payload)
        self.assertIn('"data_health"', payload)
        self.assertIn('"current_universe"', payload)


if __name__ == "__main__":
    unittest.main()
