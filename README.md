# Sniper Paper

Public-data-only, forward paper evaluation of the video-derived Bybit
liquidity/level strategy. This project is intentionally separate from the
historical recorder and does not contain authenticated trading code or API
secrets.

The frozen protocol will live in `paper_strategy_v1.json`. Paper outcomes must
keep `REJECTED` and `MISSED` candidates distinct from trades and must record
executable entry, TP/SL, costs, data quality, and restart state.
