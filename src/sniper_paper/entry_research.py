"""Price/flow observations and bar markouts; never submit orders."""

import math

VERSION = "entry_research_v1"


def observations(evaluator, symbol, bar, frame, levels, readiness):
    """Capture every active episode, before lane priority or execution gates."""
    active = {level.level_id: level for level in levels}
    for episode in evaluator.active_approaches():
        level = active.get(episode["level_id"])
        if level is None or level.broken_at_ms is not None:
            continue
        armed = episode["armed_at_ms"]
        if armed >= bar.closed_at_ms:
            continue
        same_bucket = armed > bar.opened_at_ms
        # Live state can already include the next bucket's flushing trade.
        # Never use that state as evidence for an earlier closed bucket.
        crossed_at = episode["crossed_at_ms"]
        crossed = crossed_at is not None and crossed_at <= bar.closed_at_ms
        if not same_bucket:
            crossed = crossed or (bar.high >= level.price + evaluator.tick_size if level.side.value == "HIGH"
                                  else bar.low <= level.price - evaluator.tick_size)
        high = level.side.value == "HIGH"
        for kind, direction in (("breakout", 1 if high else -1), ("reclaim", -1 if high else 1)):
            geometry = (bar.close >= level.price + evaluator.tick_size if high
                        else bar.close <= level.price - evaluator.tick_size) if kind == "breakout" else (
                            crossed and (bar.close < level.price if high else bar.close > level.price))
            delta = None if same_bucket else frame.delta_notional
            gates = {
                "geometry": geometry,
                "delta": None if delta is None or frame.median_abs_delta_20 <= 0 else (
                    direction * delta >= frame.median_abs_delta_20 if kind == "breakout"
                    else abs(delta) >= frame.median_abs_delta_20),
                "microprice": direction * frame.microprice_mid_bp > 0,
                "imbalance": direction * frame.book_imbalance >= evaluator.min_book_imbalance,
                "depth": (frame.top5_bid_notional if direction == 1 else frame.top5_ask_notional)
                >= evaluator.min_depth_notional_top5,
                "quote": frame.best_bid > level.price if direction == 1 else frame.best_ask < level.price,
            }
            yield {
                "diagnostic_id": f"{VERSION}:{episode['episode_id']}:{kind}:{bar.closed_at_ms}",
                "setup_id": str(episode["episode_id"]),
                "occurred_at_ms": frame.received_at_ms, "symbol": symbol,
                "lane": VERSION, "side": "LONG" if direction == 1 else "SHORT",
                "status": "OBSERVED", "reason": kind,
                "reference_price": level.price, "stop_price": None, "target_price": None,
                "features": {
                    "research_version": VERSION, "episode": dict(episode),
                    "bucket_close_ms": bar.closed_at_ms, "gates": gates,
                    "reference_mid": frame.mid, "reference_close": bar.close,
                    "best_bid": frame.best_bid, "best_ask": frame.best_ask,
                    "delta_notional": delta, "median_abs_delta_20": frame.median_abs_delta_20,
                    "same_bucket_delta_unavailable": same_bucket,
                    "microprice_mid_bp": frame.microprice_mid_bp, "book_imbalance": frame.book_imbalance,
                    "top5_bid_notional": frame.top5_bid_notional, "top5_ask_notional": frame.top5_ask_notional,
                    "spread_bp": frame.spread_bp, "book_age_ms": frame.book_age_ms,
                    "readiness": dict(readiness), "not_a_fill": True,
                },
            }


def markout(bars, observed_at_ms, price, side, horizon_ms):
    """Only fully subsequent 15s bars; disclose the unobserved initial interval."""
    start = math.ceil(observed_at_ms / 15000) * 15000
    end = start + horizon_ms
    sample = [b for b in bars if start <= b['opened_at_ms'] < end]
    expected = list(range(start, end, 15000))
    if [b['opened_at_ms'] for b in sample] != expected:
        return {"available": False, "reason": "missing_or_unfinished_bars"}
    direction = 1 if side == "LONG" else -1
    favorable = max(b['high'] for b in sample) if direction == 1 else min(b['low'] for b in sample)
    adverse = min(b['low'] for b in sample) if direction == 1 else max(b['high'] for b in sample)
    return {
        "available": True, "initial_unobserved_ms": start - observed_at_ms,
        "signed_return_bp": direction * (sample[-1]['close'] / price - 1) * 10000,
        "mfe_bp": max(0, direction * (favorable / price - 1) * 10000),
        "mae_bp": max(0, -direction * (adverse / price - 1) * 10000),
        "reference_crossed": any(b['low'] <= price <= b['high'] for b in sample),
    }
