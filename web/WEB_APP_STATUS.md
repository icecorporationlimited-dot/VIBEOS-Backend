# Web app status

Served by the backend at `/app` (same origin as `/login` and `/web/*`).

- Sign-in, name and Instagram connect status: live (`/web/api/me`).
- Photos: uploaded to the hosted buffer (`/web/api/media`), per user.
- Autopilot: ON/OFF, posts per day, posting times, quiet hours, time zone (`/web/api/autopilot`).
- Posting to Instagram: worker migrated to owner IDs (part C). It stays OFF until `WEB_PUBLISHING=1` is set on the server; the dashboard shows a notice while it is off.
- Reels, Trend Stories, comment replies and first comments: switches in Studio settings (need `WEB_PUBLISHING=1`).
- There is no manual queue: Autopilot decides format and time.
