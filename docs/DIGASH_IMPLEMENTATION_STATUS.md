# Digash change implementation status

Date: 2026-09-09. Protocol: `paper_strategy_v2.json` v2.4.0.

This is an implementation ledger, not evidence of profitability and not a
claim that the undisclosed Digash level algorithm has been cloned exactly.

## Implemented and locally verified

- One canonical `Level` model is shared by the chart and strategy evaluators.
- Level formations and their revisions are stored in SQLite; `broken_at_ms` is
  absorbing across recalculation and restart.
- The first breakout can reference the level state immediately before the
  break event, while later ordinary breakouts cannot reuse it.
- Previous-day levels require all 96 contiguous completed 15m bars.
- Bootstrap and in-memory retention use 1000 completed bars per timeframe and
  publish explicit short-history/gap diagnostics.
- A separate Digash reference-geometry adaptation implements the documented
  1000/40/20 contract with configurable merge/break rules. It remains shadow
  because the exact pivot, wick/close and intermediate timeframe tolerances are
  not public.
- Causal, threshold-free NATR 5m/14, signed 24h return, dollar volume, volume
  splash, volatility and BTC-correlation math is implemented with coverage and
  missing-data reasons. Daily candidates record NATR and signed return without
  changing the existing ranking.
- Daily and live books record $50k buy/sell execution VWAP, marginal impact and
  insufficient-depth status. No guessed impact threshold is a trade gate.
- Failed-sweep confirmation uses the delta baseline frozen when the setup was
  armed. Target-seeking preparation/execution decisions share a
  `parent_setup_id`. Cascade diagnostics record ordered members, adjacent gaps
  and total span without adding an unverified gate.
- Every triggered signal starts an independent public-trade TP/SL path. First
  touch, MFE, MAE, timeout and coverage are separate from paper fills and P&L;
  disconnect/restart closes an uncertain path as `UNKNOWN`.
- The dashboard exposes metric/liquidity coverage, history diagnostics,
  versions/session, active and broken levels, and a separate TP/SL path tab.

## Intentionally not promoted into trade gates yet

- Exact Digash level geometry: 20 labelled cases and a frozen 10-case holdout
  still need to be assembled from matching venue/symbol/timeframe screenshots.
- `trade_count_24h >= 1,000,000`: Bybit ticker does not provide this field. A
  wider deduplicated public-trade recorder must first be load-tested; counts
  from only the selected daily coins would be selection-biased.
- Exact $50k impact cutoff, BTC-independence cutoff, smooth-approach cutoff,
  level-age exceptions and 1%/2%/3% cascade rules remain shadow observations
  until their scale/context is unambiguous.
- `target_front`, `target_cross` and `post_cross_impulse` exits are not silently
  mixed. The existing target-level touch and fixed-2R variants remain labelled
  controls until the other exit contracts are frozen.
- Trend lines, OI, liquidation index and funding-derived entry filters are not
  required for this horizontal-level package. Funding as an actual paper cost
  remains future work for positions spanning a funding event.

## Verification performed

- Full unit suite and Ruff pass.
- Docker image builds and initializes schema v5.
- Additive migration from an older local SQLite copy preserves existing rows
  and passes `PRAGMA integrity_check`.
- Production deployment must back up the remote database, migrate a copy,
  then verify container health, schema/integrity, feed readiness and data growth.
