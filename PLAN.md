# Paper strategy implementation plan

## Boundary

- Separate server and repository.
- Public Bybit market data only; no API keys and no authenticated order path.
- One daily universe selection at the start of the UTC day; no retrospective
  additions.
- Paper only: all fills, TP/SL and P&L are simulated and journaled.

## Strategy lanes

- `early_target_hunt`: local sweep/reaction or consolidation break toward a
  pre-existing higher-timeframe target.
- `terminal_level_breakout`: consolidation at the final target followed by a
  confirmed target break.

Keep lanes, target timeframe (`4h`/`15m`), level class, and exit mode separate
in every report. Baseline exits are deterministic; manual target changes and
re-entry are diagnostics only.

## Build order

1. Freeze `paper_strategy_v1.json` and hash it.
2. Discover USDT perpetuals and build the once-daily selector with spread and
   depth gates.
3. Subscribe to orderbook/trades for selected symbols; retain compact causal
   bars, decisions, book-health events and restart evidence. Full raw depth is
   deliberately left to the separate recorder so this server stays bounded.
4. Build causal 1m/5m/15m/4h bars and horizontal level lifecycle.
5. Implement both signal lanes with long/short mirrors.
6. Implement executable paper entry, hard SL, target exit, costs and latency.
7. Add SQLite journal, health state, restart recovery and compact dashboard.
8. Add replay/unit tests, then deploy with Docker on the separate server.

## First paper gate

Run at least 14 complete UTC days without changing thresholds. Report every
candidate, rejection, missed signal, fill, exit, data gap and restart. A
positive result is not live approval; any later promotion requires an untouched
holdout and explicit review.
