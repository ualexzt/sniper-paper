# Telegram alerts

Private configuration: `/runtime/telegram.json` in the container, corresponding
to `runtime/telegram.json` on the server. Keys: `token`, `chat_id`. Permissions
must be 0600, owned by the service UID (1000). Never commit this file.

Two outbound notifications:

- ARMED level episode after stream continuity and warmup: proximity alert,
  deduplicated across timeframes at the same symbol/side/price for 15 minutes.
- Valid strategy signal: BUY/SELL, reference level, signal price, setup type,
  Kyiv timestamp. Emitted before portfolio/order execution checks; not a fill.

Delivery runs on a separate bounded worker queue. Messages older than 60 seconds
are dropped. Successful sends are deduplicated in `/runtime/telegram-alerts.sqlite`
across restarts. Network failures are logged without URLs or credentials.
Delivery is best effort: ambiguous timeouts are not retried automatically to
avoid duplicates. Missing private configuration disables Telegram.

There are no inbound commands and no authenticated exchange integration.
