from pathlib import Path

import pytest

from sniper_paper.app import PaperApp, SymbolState, _parse_klines
from sniper_paper.market import Bar
from sniper_paper.storage import Journal
from sniper_paper.strategy import Level, LevelSide


def test_parse_klines_orders_oldest_first_and_excludes_forming() -> None:
    rows = [
        [120_000, "3", "4", "2", "3.5", "5", "17"],
        [60_000, "2", "3", "1", "2.5", "4", "10"],
        [0, "1", "2", "0.5", "1.5", "3", "4"],
    ]
    bars = _parse_klines("XUSDT", "1m", rows, 150_000)
    assert [item.opened_at_ms for item in bars] == [0, 60_000]
    assert bars[-1].close == 2.5


def test_market_detail_exposes_real_state_and_rejects_out_of_universe(tmp_path: Path) -> None:
    app = PaperApp(Journal(tmp_path / "paper.db"))
    state = SymbolState("XUSDT")
    state.bars["5m"] = [Bar("XUSDT", 300_000, 0, 300_000, 100, 102, 99, 101, 10, 1, 2)]
    state.levels = [Level("l1", "XUSDT", "4h", LevelSide.HIGH, 110, 1)]
    state.book.apply(
        {
            "type": "snapshot",
            "data": {
                "s": "XUSDT",
                "u": 1,
                "seq": 1,
                "b": [["100", "2"]],
                "a": [["102", "3"]],
            },
        },
        1,
    )
    app.states = {"XUSDT": state}
    result = app.market_detail("XUSDT", "5m")
    assert result["bars"][0]["close"] == 101
    assert result["levels"][0]["level_id"] == "l1"
    assert result["quote"] == {"bid": 100.0, "ask": 102.0, "mid": 101.0}
    with pytest.raises(ValueError, match="daily universe"):
        app.market_detail("OTHERUSDT", "5m")
