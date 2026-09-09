# sniper-paper operational handoff

Last updated: 2026-09-09 (Europe/Kyiv)

## Safety boundary

- This repository is public-market-data-only and paper-only.
- It must not receive exchange credentials or submit live orders.
- Remote host: `ubuntu@54.154.79.239`.
- Remote checkout: `/home/ubuntu/sniper-paper`.
- Dashboard: loopback-only port `8080` (use an SSH tunnel).

## Release v2.4.0

- Canonical durable level catalogue and absorbing broken-level lifecycle.
- Shadow Digash 1000/40/20 reference geometry; it is not a trade gate.
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

- Deployed code commit: `f40cb52467b8d0e10806c4ee44cee61322f88633`.
- Deployed at: 2026-09-09 10:49 UTC.
- Pre-release database backup:
  `runtime/paper.db.pre-v2.4.0-20260909T1049Z`.
- Post-deploy checks: container healthy, restart count 0, OOM false,
  WebSocket connected, 8/8 books ready, dashboard HTTP 200, SQLite
  `integrity_check=ok`, schema version 5.
- The 2026-09-09 session is intentionally `partial_day_observation_only`
  because the protocol changed mid-day. Forward evaluation can become eligible
  only after the next complete UTC-day selection and warmup.
