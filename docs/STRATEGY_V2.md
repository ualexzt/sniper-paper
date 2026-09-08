# Strategy V2

Frozen specification version: v2.2.0, dated 2026-09-08.

This document defines the paper-only v2 evaluator foundation. It does not place
orders, does not depend on the live app parser, and only consumes immutable
causal inputs.

## Scope

The evaluator emits one deterministic decision per lane from the following set:

- `failed_sweep_reclaim`
- `early_target_hunt`
- `terminal_level_breakout`
- `target_seeking_breakout`
- `structural_reaction`
- `cascade_impulse`
- `fresh_extreme_momentum`
- `dom_confirmed_breakout`
- `diagonal_context`

`dom_confirmed_breakout` and `diagonal_context` are diagnostic only. They must
never emit tradable signals.

## Input Contract

The evaluator accepts immutable input dataclasses:

- `Level`
- `OrderflowFrame`
- completed bar sequences keyed by timeframe such as `15s`, `1m`, `5m`,
  `15m`, and `4h`

No decision may read bars or orderflow with timestamps later than the
evaluation timestamp. Future data is ignored, not backfilled.

### Level lifecycle

A confirmed horizontal level remains eligible until its first causal,
close-based break. A HIGH breaks above and a LOW breaks below by at least one
instrument tick when either of these conditions is first met:

- two consecutive completed 1m candles close beyond the level; or
- one completed candle on the level's own 15m/4h source timeframe closes
  beyond it.

A wick beyond the price and a single 1m close followed by a reclaim do not
break the level. This preserves the failed-sweep/reclaim hypothesis. Once
broken, the level is absorbing: it cannot reactivate when price returns. The
same `broken_at_ms` cutoff is applied before target selection and before chart
rendering. Bootstrap history reconstructs the cutoff after a restart; bars
closed before level confirmation or after evaluation time are ignored.

The required orderflow frame is intentionally explicit. It carries:

- executable best bid and ask
- top-of-book sizes for microprice
- top-5 notional imbalance
- 15s delta and range statistics
- ATR context
- book age and spread
- sweep and structure reference prices when available

## Status Model

The public decision statuses are only:

- `REJECTED`
- `MISSED`
- `TRIGGERED`

Internally, a lane may be armed while waiting for confirmation. That armed state
is not a separate public status; it is represented as `REJECTED` with a reason
such as `armed_pending_confirmation` or `waiting_confirmation`.

`MISSED` means the setup expired, was already attempted, or was otherwise no
longer eligible under the one-attempt rule.

## Causal State Transitions

The evaluator uses a simple deterministic lifecycle:

1. `REJECTED` when the lane does not yet have enough causal evidence.
2. `REJECTED` with an armed reason when a setup is waiting for confirmation.
3. `TRIGGERED` when the confirmation conditions are satisfied before expiry.
4. `MISSED` when TTL expires or the setup has already been consumed.

Each setup is keyed by lane, symbol, and side. That keeps the lanes isolated and
prevents one lane from consuming another lane’s state.

## Threshold Policy

Thresholds that are defined in `TRADING_RULES_V1.md` are reused directly:

- 20 prior 15s intervals for sweep statistics
- 14 completed 1m bars for ATR
- 5-minute post-snapshot warmup
- 500 ms max book age
- 2 bp max spread
- 5% minimum 4h trend move
- 6-bar 5m consolidation
- 250 bp max consolidation width
- 0.05 minimum book imbalance
- 1.5 minimum reward/risk for the conservative breakout lanes
- 2.0 terminal reward/risk
- 5 bp stop buffer
- 10 to 50 bp permitted risk band
- 250 ms entry latency
- 2 s entry TTL
- 30 s sweep confirmation window
- 9.5 bp budgeted execution cost

Anything not defined by v1 is treated as a predeclared conservative hypothesis.

## Lane Notes

`failed_sweep_reclaim` is the primary lane. It arms on a causal sweep, then
triggers only if the later confirmation bar reclaims the level without trading
back through the sweep extreme.

`early_target_hunt` is the preparatory consolidation watcher. It can arm a
setup but delegates the actual breakout execution to `target_seeking_breakout`.

`target_seeking_breakout` is the executable breakout continuation lane for the
same consolidation context.

`terminal_level_breakout`, `structural_reaction`, `cascade_impulse`, and
`fresh_extreme_momentum` are tradable hypotheses with conservative defaults.

`dom_confirmed_breakout` and `diagonal_context` remain diagnostic only.

## Shadow diagnostics

Version v2.2 adds three observation-only hypotheses:

- `retest_reclaim_v1` observes a causal close-based level break, a later
  first retest on completed 1m bars, and a subsequent break of the retest
  reaction range. Its stop and nearest-liquidity target are measurements, not
  executable instructions.
- `density_bounce_v1` requires a persistent public-book wall with observable
  age, initial size, and remaining size. Missing fields and ambiguous removal
  fail closed. A remaining ratio at or below 0.5 invalidates the observation.
- `cascade_terminal_exit` measures strong completed 1m impulse bars against
  levels that were causal before the impulse bar closed.

These rows are persisted only in `shadow_diagnostics`. They never create a
`StrategySignal`, never enter `signals`, and have no path to `paper_orders` or
`positions`. Stable diagnostic identifiers provide restart-safe deduplication.
The first partial UTC day after a new protocol deployment is retained for
observation but marked `evaluation_eligible=false`; complete-day evaluation
starts from the next UTC anchor.

## Unvalidated Hypotheses

The following are not claimed as profitable or validated:

- structural reaction around causal 15m levels
- cascade impulse continuation after strong body and delta alignment
- fresh extreme momentum after a causal higher-timeframe extreme
- DOM-confirmed breakout as a diagnostic confirmation layer
- diagonal context as a diagnostic-only regime feature

The file [paper_strategy_v2.json](../paper_strategy_v2.json) holds the machine
readable thresholds for the frozen evaluator.
