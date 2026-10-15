# Per-user rate limiting (Telegram bot)

`ratelimit.py` is registered in handler group -1 in `bot.py`. It only blocks a
user who exceeds a limit; no existing handler or feature is changed. Limits are
per Telegram user, so one user cannot slow down others. A blocked user gets one
notice per 30 seconds, not a reply per message.

Defaults (count/seconds), override with environment variables:

| Variable | Default | Applies to |
|---|---|---|
| RATE_LIMIT_COMMANDS | 25/60 | slash commands |
| RATE_LIMIT_CALLBACKS | 60/60 | inline button taps |
| RATE_LIMIT_PHOTOS | 60/600 | photos (a 10-photo album is fine) |
| RATE_LIMIT_MESSAGES | 30/60 | other messages |

State is in-memory (resets on restart; single instance). If you ever run more
than one bot instance, move the counters to MongoDB or Redis.
