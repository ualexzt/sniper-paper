# sniper-paper operational handoff

Last updated: 2026-09-09 (Europe/Kyiv)

## Safety boundary

- This repository is public-market-data-only and paper-only.
- It must not receive exchange credentials or submit live orders.
- Remote host: `ubuntu@54.154.79.239`.
- Remote checkout: `/home/ubuntu/sniper-paper`.
- Dashboard: loopback-only port `8080` (use an SSH tunnel).

## Release v2.5.1

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

- Deployed code commit: `7b765ce522dba255b3a552ae74745b131b269812`.
- Deployed at: 2026-09-09 16:15 UTC.
- Pre-release database backup:
  `runtime/paper.db.pre-v2.5.1-20260909T161538Z`.
- Post-deploy checks: container healthy, restart count 0, OOM false,
  WebSocket connected, 8/8 books ready, dashboard HTTP 200, SQLite
  `integrity_check=ok`, schema version 5. Active and broken
  `digash_horizontal_levels_v2` rows exist on every one of the seven
  timeframes; the read-only market API exposes only that level version.
- Capture growth check: completed 15s rows increased from 65,337 to 65,361.
  New-process heartbeats were 30 seconds apart, books 8/8, HTTP health 200.
  CPU snapshots decreased from about 100% before deployment to 24% and 20%
  after deployment. These are short operational checks, not a long soak test.
- Local regression suite and Ruff passed. Independent reproductions cover
  constituent/cluster resurrection, restart, version isolation, source-vs-fast
  closes, incomplete buckets and batched durable upserts. An 8,000-trade bar
  benchmark decreased from 5.21s to 0.039s; 1,000 unchanged level upserts took
  0.023s in a batch versus 0.361s individually on the local machine.
- The 2026-09-09 session is intentionally `partial_day_observation_only`
  because the protocol changed mid-day. Forward evaluation can become eligible
  only after the next complete UTC-day selection and warmup.
