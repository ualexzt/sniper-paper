# Paper strategy implementation plan

## Boundary

- Separate server and repository.
- Public Bybit market data only; no API keys and no authenticated order path.
- One daily universe selection at the start of the UTC day; no retrospective
  additions.
- Paper only: all fills, TP/SL and P&L are simulated and journaled.

## Strategy lanes (v2)

- Primary: `failed_sweep_reclaim`.
- Target/breakout: `early_target_hunt`, `terminal_level_breakout`,
  `target_seeking_breakout`, `cascade_impulse`, `fresh_extreme_momentum`.
- Reaction: `structural_reaction`.
- Diagnostic only: `dom_confirmed_breakout`, `diagonal_context`.

Keep lanes, target timeframe (`4h`/`15m`), level class, and exit mode separate
in every report. Baseline exits are deterministic; manual target changes and
re-entry are diagnostics only.

## Build order

1. Freeze `paper_strategy_v2.json` and hash it. Completed.
2. Discover USDT perpetuals and build the once-daily selector with spread and
   depth gates.
3. Subscribe to orderbook/trades; build causal 15s footprint and uncertainty-
   preserving DOM evidence with snapshot/gap warmup. Completed.
4. Build causal bars and horizontal level lifecycle. Completed.
5. Implement nine isolated long/short v2 lanes. Completed as frozen paper
   hypotheses; diagnostics never place simulated orders.
6. Implement strict-queue post-only entry, partial fills, TTL, hard SL/TP,
   fees/slippage and restart persistence. Completed.
7. Expose readiness and order flow on the read-only dashboard. Completed.
8. Unit/synthetic tests are complete; Docker and deployed forward-paper
   verification remain the release gate.

## First paper gate

Run at least 14 complete UTC days without changing thresholds. Report every
candidate, rejection, missed signal, fill, exit, data gap and restart. A
positive result is not live approval; any later promotion requires an untouched
holdout and explicit review.
