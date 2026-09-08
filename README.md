# Sniper Paper

Public-data-only, forward paper evaluation of the video-derived Bybit
liquidity/level strategy. This project is intentionally separate from the
historical recorder and does not contain authenticated trading code or API
secrets.

The active frozen protocol lives in `paper_strategy_v2.json`; v1 is retained
for provenance. Paper outcomes must
keep `REJECTED` and `MISSED` candidates distinct from trades and must record
executable entry, TP/SL, costs, data quality, and restart state.

For a plain-language Ukrainian explanation, see
[`docs/STRATEGY_UA.md`](docs/STRATEGY_UA.md).

## What runs

- A once-per-UTC-day selector ranks Bybit USDT linear perpetuals from public
  ticker data, then applies spread/depth gates to a bounded candidate pool.
- If the service is first started more than five minutes after UTC midnight,
  that partial day is observation-only; paper evaluation starts with the next
  complete daily universe.
- Public `orderbook.50` and `publicTrade` streams maintain executable quotes,
  causal 15-second footprint buckets, DOM evidence, receive-time bars, and
  causal 15m/4h levels. An application-level WebSocket ping avoids the prior
  recurring library keepalive disconnects.
- A level is removed from both future target selection and the chart after a
  causal close-through: two consecutive completed 1m closes or one completed
  source-timeframe close at least one tick beyond it. Wicks do not invalidate
  levels, and broken levels never reactivate after price returns.
- V2 keeps nine named lanes isolated. `failed_sweep_reclaim` is primary;
  breakout/reaction lanes remain separate hypotheses, while
  `dom_confirmed_breakout` and `diagonal_context` are diagnostic-only.
- Entry is a simulated post-only order after 250 ms. It freezes the signal-time
  best price, waits behind displayed queue, uses only exact-price opposing
  public trades for fills, supports partial fills, and cancels the remainder
  after a two-second TTL. Book cancellations never invent a fill.
- SQLite schema v2 stores the frozen universe and instrument increments, bars,
  actionable decisions, paper orders/queue state, positions, costs, P&L, and
  service events across restarts.
- A stream break invalidates every book and any pending entry becomes `MISSED`;
  trading waits for a new snapshot and warmup.
- The read-only trading dashboard has a daily-universe sidebar, selectable
  5m/15m/4h candlestick chart with causal 15m clusters, previous-day levels,
  4h swings and paper brackets, plus Paper trading, Open orders and History
  tabs. Its header reports server heartbeat, public WebSocket state and book
  readiness; a prominent READY/BLOCKED gate and compact order-flow panel make
  the current eligibility, warmup, delta, microprice and DOM evidence visible.
- The chart supports cursor-centered wheel zoom, pointer/touch drag panning,
  arrow/zoom controls and reset. Horizontal level rays begin at their actual
  pivot candle (or the price-defining pivot of a cluster) and extend right.

## Local verification

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
docker compose build
```

Run locally:

```bash
.venv/bin/sniper-paper --database runtime/paper.db --dashboard-port 8080
```

The production Compose mapping binds the dashboard to server loopback. View it
through an SSH tunnel rather than opening a public firewall port:

```bash
ssh -N -L 8080:127.0.0.1:8080 ubuntu@SERVER
```

Then open `http://127.0.0.1:8080/`.

## Safety

There is no private REST/WebSocket endpoint, API-key field, signing code, or
order route in this repository. `REJECTED` and `MISSED` are non-trades. The
default parameters are frozen hypotheses and must not be silently tuned after
forward outcomes are visible.
