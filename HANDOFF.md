# sniper-paper operational handoff

## Release v2.7.0 — level touch episodes (2026-09-10)

- The Digash level detector is bumped to
  `digash_horizontal_levels_v3_touch_episodes`.
- Equal separated extrema, continuous plateaus, causal return episodes, and
  absorbing break cutoffs are implemented and version-labelled. Cross-timeframe
  exact-price grouping is presentation-only; the trading catalogue is unchanged.
- The exact Digash pivot, touch-separation, and intermediate-TF tolerance rules
  remain unpublished. These choices are explicitly versioned hypotheses.
- Deployment verification is recorded below after rollout.

Last updated: 2026-09-10 (Europe/Kyiv)

## Safety boundary

- This repository is public-market-data-only and paper-only.
- It must not receive exchange credentials or submit live orders.
- Remote host: `ubuntu@54.154.79.239`.
- Remote checkout: `/home/ubuntu/sniper-paper`.
- Dashboard: loopback-only port `8080` (use an SSH tunnel).

## Release v2.6.0

- Adds paper-only `paper_profit_protection_shadow_v2` counterfactual exits.
- The price-only path activates from net executable PnL at 1R and records an
  exit after a 0.5R giveback from its monotonic high-water floor. The second
  path also requires normalized adverse aggressive delta and a completed 15s
  close through the preceding bucket's protective extreme.
- Both paths include entry/exit fees and configured exit slippage, resume from
  persisted MFE after restart, and settle against the baseline result when no
  earlier shadow exit occurs. Incomplete, reconnect and warmup footprints do
  not confirm orderflow exits.
- Thresholds are frozen hypotheses; baseline fixed SL/TP and execution are
  unchanged.
- Deployed code commit: `4fdfc40` at 2026-09-10 12:57 UTC. Pre-release database
  backup: `runtime/paper.db.pre-v2.6.0-20260910T125645Z`; a separate migrated
  copy passed SQLite integrity and schema-6 checks before restart.
- Post-deploy: container healthy, restart count 0, OOM false, WebSocket
  connected, 10/10 books ready, API HTTP 200 and SQLite integrity check `ok`.
  The existing PONSUSDT position restored; its first price-only observation is
  retained, but the mid-day protocol change makes this UTC session observation
  only. Forward evaluation can start after the next complete UTC selection.

## Release v2.5.2

- Every triggered v2 lane validates positive finite SL/entry/TP and the
  configured 10–50 bp risk band using current executable entry.
- The application also validates the signal-path reference before starting
  a tracker. Invalid geometry is journaled as REJECTED without interrupting
  the public stream.
- Queue arithmetic suppresses floating-point residue using operand precision,
  while preserving genuine small partial fills.
- Position history derives numerical-dust and stream-gap/restart annotations
  without rewriting the original rows. Dashboard hides dust trades and marks
  gap-affected exits. Raw accounting remains available; quality-filtered lane
  results are separate and do not establish complete source-data coverage.
- Strategy/execution versions changed; a mid-day deployment remains
  observation-only until the next complete UTC selection under this protocol.
- Digash selection, cascade/fresh-extreme semantics and exact exit rules are
  still unresolved as documented in `docs/AUDIT_2026-09-10.md`.

## Previous release v2.5.1

- Fixes the v2.5.0 cluster-resurrection defect by invalidating constituents
  before merging and retaining broken-cluster member tombstones. Runtime
  level version is `digash_horizontal_levels_v2`; IDs include the version,
  preserving prior rows for audit.
- Requests historical bars strictly before the current forming bucket.
  Startup/reconnect partial receive-time buckets are suppressed. This can
  leave explicit history gaps until the next exchange-history bootstrap.
- OHLCV aggregation is incremental with bounded deduplication; SQLite level
  upserts share one transaction per batch instead of one per level.

- Runtime Digash horizontal levels replace the legacy 15m/4h-only catalogue
  across `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, and `1d`.
- Each timeframe uses up to 1,000 completed bars, a versioned centred
  unique-extremum 40-bar hypothesis, a 20-bar right exclusion, documented
  merge-tolerance endpoints, and a versioned intermediate-tolerance hypothesis.
- Canonical durable level catalogue and absorbing completed-close broken-level
  lifecycle; broken levels cannot reactivate after recalculation or restart.
- Causal observation-only metrics and book-liquidity diagnostics.
- Independent public-trade TP/SL signal-path tracking.
- Dashboard level history, metrics, liquidity, versions and TP/SL paths.
- SQLite schema version 5; migration is additive.

Exact unpublished Digash thresholds and geometry are deliberately not guessed.
See `docs/DIGASH_IMPLEMENTATION_STATUS.md` for the evidence gaps.

## Deployment checklist

1. Confirm local Ruff, pytest, Docker build and image smoke tests.
2. Confirm the remote checkout is clean and fast-forward it.
3. Back up `runtime/paper.db` before any migration or restart.
4. Build the image and migrate a copy of the database first.
5. Restart with Docker Compose, then verify health, restart/OOM state,
   SQLite integrity/schema, API payloads, feed readiness and capture growth.
6. A mid-day protocol change is observation-only until a complete subsequent
   UTC session; do not interpret that state as a strategy failure.

## Known operational caveat

The remote public stream has shown recurring disconnect/reconnect events. A
disconnect invalidates books, makes pending entries `MISSED`, and closes active
signal paths as `UNKNOWN`. Treat reconnect frequency as an independent data
quality/evaluation blocker until it is separately diagnosed.

## Current deployment

- Deployed code commit: `2c73c24`.
- Deployed at: 2026-09-10 13:46 UTC.
- Protocol v2.7.0 and level version
  `digash_horizontal_levels_v3_touch_episodes` are active. The first rebuild
  produced 2,058 versioned level rows, including 476 active rows; the dashboard
  continues to return only its nearest presentation set and groups exact-price
  cross-TF duplicates without changing the trading catalogue.
- Dashboard-only follow-up: the right workspace now stays within the viewport,
  the paper-trading panel is pinned to the bottom, and the chart consumes the
  remaining height. Retired readiness, verified-metrics, active-level-list and
  orderflow blocks were removed from the operator UI; level rays remain on the
  chart and research diagnostics remain available through the read-only API.
- Pre-release database backup:
  `runtime/paper.db.pre-v2.7.0-20260910T134505Z` (`integrity_check=ok`, schema 6).
  A separate v2.7.0 migration-test copy also passed integrity and schema checks.
- Post-deploy checks: container healthy, restart count 0, OOM false,
  WebSocket connected, 10/10 books ready, HTTP health 200, SQLite
  `integrity_check=ok`, schema version 6. The existing PONSUSDT SHORT position
  restored with the same position ID and bracket. After the initial 0/10
  startup heartbeat, three subsequent heartbeats showed 10/10 books and no
  restart/disconnect event.
- Local Ruff, 183 tests, a fresh Docker build and image smoke test passed.
- The 2026-09-10 session is intentionally `partial_day_observation_only`
  because the protocol changed mid-day. Forward evaluation can become eligible
  only after the 2026-09-11 UTC selection and warmup.
