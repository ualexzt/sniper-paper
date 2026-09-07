# Sniper Paper

Public-data-only, forward paper evaluation of the video-derived Bybit
liquidity/level strategy. This project is intentionally separate from the
historical recorder and does not contain authenticated trading code or API
secrets.

The frozen protocol lives in `paper_strategy_v1.json`. Paper outcomes must
keep `REJECTED` and `MISSED` candidates distinct from trades and must record
executable entry, TP/SL, costs, data quality, and restart state.

## What runs

- A once-per-UTC-day selector ranks Bybit USDT linear perpetuals from public
  ticker data, then applies spread/depth gates to a bounded candidate pool.
- If the service is first started more than five minutes after UTC midnight,
  that partial day is observation-only; paper evaluation starts with the next
  complete daily universe.
- Public `orderbook.50` and `publicTrade` streams maintain executable quotes,
  receive-time bars, causal 15m/4h levels, and flow confirmation.
- `early_target_hunt` and `terminal_level_breakout` remain separate paper lanes.
- SQLite stores the frozen universe, bars, all signal decisions, open/closed
  paper positions, MFE/MAE, costs, P&L, and service events.
- A stream break invalidates every book and any pending entry becomes `MISSED`;
  trading waits for a new snapshot and warmup.
- The read-only trading dashboard has a daily-universe sidebar, selectable
  5m/15m/4h candlestick chart with causal 15m clusters, previous-day levels,
  4h swings and paper brackets, plus Paper trading, Open orders and History
  tabs. Its header reports server heartbeat, public WebSocket state and book
  snapshot readiness; it exposes no order or mutation controls.
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
