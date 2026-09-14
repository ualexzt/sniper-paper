from sniper_paper.entry_research import markout, observations
from test_strategy_v2 import build_level_approach_fixture
from sniper_paper.strategy_v2 import StrategyV2Evaluator


def test_records_both_alternatives_and_actual_depth_without_executing():
    f = build_level_approach_fixture('rejection')
    e = StrategyV2Evaluator()
    e.observe_price(symbol='XUSDT', now_ms=1_200_000, price=100.05, levels=[f['level']])
    rows = list(observations(e, 'XUSDT', f['bars']['15s'][-1], f['frame'], [f['level']], {'ready': True}))
    assert len(rows) == 2
    reclaim = next(r for r in rows if r['reason'] == 'reclaim')
    assert reclaim['features']['gates']['geometry'] is True
    assert reclaim['features']['top5_bid_notional'] == 2000
    assert reclaim['features']['not_a_fill'] is True
    assert len(e.active_approaches()) == 1


def test_same_bucket_delta_is_unknown_and_future_cross_not_used():
    f = build_level_approach_fixture('breakout')
    e = StrategyV2Evaluator()
    e.observe_price(symbol='XUSDT', now_ms=1_215_005, price=99.95, levels=[f['level']])
    rows = list(observations(e, 'XUSDT', f['bars']['15s'][-1], f['frame'], [f['level']], {}))
    assert all(r['features']['gates']['delta'] is None for r in rows)


def test_markout_excludes_partial_bar_and_refuses_gap():
    bars = [{'opened_at_ms': t, 'high': 102, 'low': 99, 'close': 101} for t in (0, 15000, 30000)]
    result = markout(bars, 1, 100, 'LONG', 30000)
    assert result['available']
    assert result['initial_unobserved_ms'] == 14999
    assert round(result['signed_return_bp']) == 100
    assert not markout(bars[:-1], 1, 100, 'LONG', 30000)['available']
