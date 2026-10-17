# Part C: worker migration (telegram_id -> owner_id)

The background worker now works on verified `Principal`s (`tg:<id>` or `web:<id>`),
so web dashboard users get the same automation as Telegram users.

## Turning it on

Web publishing is **off by default**. Nothing changes for Telegram users while it is off.

1. Deploy this version. Check the logs: the worker starts, no web job is touched.
2. On Render set `WEB_PUBLISHING=1` and redeploy. The log shows
   `Web publishing is ENABLED`.
3. Sign in to the web app with YOUR OWN account, connect Instagram, switch Autopilot on,
   upload 2-3 photos. Expect: photos become "Ready", then "Scheduled", then "Posted".
4. Only then invite other users.

To roll back, set `WEB_PUBLISHING=0`. Queued web jobs simply wait; none are lost or posted.

## What the worker does per owner (web and Telegram alike)

| Step | Notes |
|---|---|
| Run due jobs | token, assets and slots resolved from the job's owner; a web job can never use another owner's token |
| Autofill | ready photos -> photo / story / carousel / reel routes -> next free slot in the owner's time zone |
| Event photos | festival/poster date detection pins the post to its calendar window (same planner as Telegram) |
| Trend Stories | opt-in switch; at most one a day, sourced and safety-checked |
| Comment replies / first comment | opt-in switches; only on the owner's own posts, 3 replies per pass, 10 a day |
| Token care | web tokens are refreshed before expiry; a dead login shows as account status in the dashboard |
| Notifications | Telegram owners get messages; web owners see status in the dashboard (no push channel yet) |

## Files changed

- `publishing/identity.py` - `principal_from_owner_id`, `document_principal` (fail-closed owner of any stored document)
- `publishing/service.py` - jobs resolve their owner; `handles_web_owners` gate; token/asset access via `*_for(principal)`
- `publishing/scheduler.py` - every per-user step runs on a `Principal`; `web_owners` switch
- `publishing/policy.py`, `publishing/slots.py`, `content_planner.py` - time zone and ownership per principal (`make_tz_for`, `resolve_tz`)
- `publishing/accounts.py`, `publishing/autopilot.py` - web-aware listing helpers (`enabled_principals`, `expiring_soon(include_web=)`, `mark_alerted_for`)
- `trends/auto_story.py`, `content_buffer.py` - owner-scoped trend Story queries (`owner_query`)
- `publishing/web_studio.py`, `web/index.html`, `web/app.js` - Reels / Trend Stories / replies / first-comment switches
- `publish_bot.py`, `settings.py`, `render.yaml` - `WEB_PUBLISHING` flag, principal-aware time zones
- `tests/test_worker_web_owners.py` (new), `tests/test_web_studio.py` (updated)

## One thing to know about the database

`engagement_events` used a plain unique index on `(telegram_id, event_key)`. On start the
worker replaces it with a partial index (only rows that have `telegram_id`) and adds the
same for `owner_id`. This is wrapped in try/except, so a failure is logged, not fatal.
It has NOT been run against a real MongoDB yet; check the first start's logs.

## Not done (still on the rollout checklist)

- DM auto-reply. Only comment replies exist; there is no DM code in the project.
- Rate limits on login / OAuth endpoints.
- Mongo integration tests and Meta/Instagram sandbox tests (current tests use in-memory fakes).
- An independent security review.
