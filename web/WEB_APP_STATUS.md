# Web app status

Served by the backend at `/app` (same origin as `/login` and `/web/*`).

- Sign-in, name and Instagram connect status: live (`/web/api/me`).
- Photos: uploaded to the hosted buffer (`/web/api/media`), per user.
- Autopilot: ON/OFF, posts per day, posting times, quiet hours, time zone (`/web/api/autopilot`).
- Posting to Instagram: pending worker migration (part C). The dashboard shows a notice until then.
- There is no manual queue: Autopilot decides format and time.
