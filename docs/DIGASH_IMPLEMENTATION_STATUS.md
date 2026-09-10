# Digash change implementation status

Date: 2026-09-10. Protocol: `paper_strategy_v2.json` v2.5.2.

Execution follow-up: uniform executable-entry bracket/risk admission and
numerical fill-residue protection. These fixes do not establish Digash
fidelity for cascade/fresh-extreme or validate alternative TP/SL rules.
See `AUDIT_2026-09-10.md` for the observed trades and unresolved differences.

This is an implementation ledger, not evidence of profitability and not a
claim that the undisclosed Digash level algorithm has been cloned exactly.

## Implemented and locally verified

- v2.5.1 fixes constituent-before-merge invalidation, durable member tombstones,
  and version-isolated level IDs. Source and fast confirming bars are separate.
- Bootstrap requests end before the forming bucket. Startup/reconnect partial
  receive-time buckets are quarantined; resulting gaps are not filled with
  fabricated OHLCV. Subsequent bootstrap restores exchange history.
- OHLCV aggregation is incremental with bounded trade-ID deduplication; level
  persistence batches records into one transaction per refresh.

- One canonical `Level` model is shared by the chart and strategy evaluators.
- Level formations and their revisions are stored in SQLite; `broken_at_ms` is
  absorbing across recalculation and restart.
- The first breakout can reference the level state immediately before the
  break event, while later ordinary breakouts cannot reuse it.
- Previous-day levels require all 96 contiguous completed 15m bars.
- Bootstrap and in-memory retention use 1000 completed bars per timeframe and
  publish explicit short-history/gap diagnostics.
- The runtime active-level detector covers all seven Digash timeframes:
  `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, and `1d`. It uses 1,000 completed bars
  per timeframe, a 40-bar centred unique-extremum hypothesis, and excludes the
  latest 20 bars from candidate search. The former 15m/4h-only engines are no
  longer runtime sources.
- Merge endpoints are the documented 0.20% at 1m and 1.25% at 1d. Values for
  intermediate timeframes use a versioned log-time interpolation hypothesis;
  they are not claimed to be official Digash values.
- Runtime levels use a causal completed-close lifecycle: a confirmed level is
  active until its first qualifying close break, then becomes absorbing
  `broken` and cannot reactivate after recalculation or restart. This is our
  explicit lifecycle contract, not a claim that the undisclosed Digash break
  rule has been recovered exactly.
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

- Exact Digash geometry is not claimed: the precise pivot, wick/close break
  semantics and intermediate tolerance table remain undisclosed. Twenty
  labelled cases and a frozen 10-case holdout still need to be assembled from
  matching venue/symbol/timeframe screenshots.
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
- Deployment status is intentionally not claimed here. Before any deployment,
  back up the remote database, migrate a copy, then verify container health,
  schema/integrity, feed readiness and data growth.
