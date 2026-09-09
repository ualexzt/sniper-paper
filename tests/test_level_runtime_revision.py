from pathlib import Path

from sniper_paper.app import PaperApp, SymbolState
from sniper_paper.levels import LevelSide, canonical_level_catalog
from sniper_paper.market import Bar
from sniper_paper.shadow_levels import GeometryConfig, build_reference_levels
from sniper_paper.storage import Journal


def sample():
    return [
        Bar("XUSDT", 60_000, i * 60_000, (i + 1) * 60_000,
            100, 110 if i == 40 else (110.02 if i == 65 else 101),
            99, 110.02 if i == 65 else 100, 1, 0, 1)
        for i in range(120)
    ]


def test_runtime_broken_pivot_cannot_return_through_new_cluster_or_restart(tmp_path: Path):
    journal = Journal(tmp_path / "paper.db")
    app = PaperApp(journal)
    state = SymbolState("XUSDT", tick_size=0.01)
    rows = sample()
    for count in (61, 66, 86, 120):
        state.bars["1m"] = rows[:count]
        app._refresh_digash_levels(state, count * 60_000)
        app._refresh_level_lifecycle(state, count * 60_000)
        catalog = canonical_level_catalog(state.levels, state.symbol, count * 60_000)
        if count == 61:
            assert any(level.price == 110 for level in catalog)
        else:
            assert not any(level.side is LevelSide.HIGH and level.price == 110 for level in catalog)
    expected = {level.level_id for level in catalog}
    restored_app = PaperApp(Journal(journal.path))
    restored = SymbolState("XUSDT", tick_size=0.01)
    restored.bars["1m"] = rows
    restored.levels = [restored_app._level_from_storage(row) for row in journal.load_levels()]
    restored_app._refresh_digash_levels(restored, 120 * 60_000)
    restored_app._refresh_level_lifecycle(restored, 120 * 60_000)
    assert {level.level_id for level in canonical_level_catalog(restored.levels, restored.symbol, 120 * 60_000)} == expected


def test_detector_versions_do_not_overwrite_existing_level_rows(tmp_path: Path):
    app = PaperApp(Journal(tmp_path / "paper.db"))
    old = build_reference_levels(sample()[:61], timeframe="1m", config=GeometryConfig(level_version="old"))
    new = build_reference_levels(sample()[:61], timeframe="1m", config=GeometryConfig(level_version="new"))
    assert {level.level_id for level in old.levels}.isdisjoint({level.level_id for level in new.levels})
    app.journal.upsert_levels([app._level_storage_mapping(level, 61 * 60_000) for level in (*old.levels, *new.levels)])
    assert len(app.journal.load_levels(version="old")) == len(old.levels)
    assert len(app.journal.load_levels(version="new")) == len(new.levels)
