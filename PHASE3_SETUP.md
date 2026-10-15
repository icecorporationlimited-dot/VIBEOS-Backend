# Phase 3 — Scheduler + Policy Engine

No new settings or packages. Deploy, then in Telegram:

1. `/autopilot` — see rules. Defaults: 2 posts/day, quiet 23:00–07:00, 60 min gap, slots 12:30 + 19:30.
2. `/schedule` — queue the latest ready photo in the next free slot (or `/autopilot on` to auto-schedule all ready photos).
3. `/queue` · `/cancel <id>` · `/release <id>` (held job) · `/pause` · `/resume`

Rules (publishing/policy.py): paused, format allowed, blocked topics, duplicate content,
quiet hours, daily limit, min gap. Manual /post skips pause/quiet/limit, never the safety checks.
After a crash/restart a job mid-publish resumes when its 10-minute lease expires; Instagram is asked first,
and a post that is already live is flagged (needs_attention), never re-posted.
Times use the timezone saved in memory.py (default Asia/Kolkata).
