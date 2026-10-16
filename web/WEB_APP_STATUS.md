# VIBEOS Creator Studio frontend

This is the first responsive Instagram-style web dashboard milestone. It is a static frontend and preserves the existing `/api/caption` and `/api/edit` utility API contract for future integration.

## Current behavior
- Responsive dashboard navigation: Overview, Media Library, Publishing Queue, Notifications, Settings.
- Local image preview library (up to 4 MB per image) with delete actions.
- Browser-only queue drafts with content type, caption, and optional schedule.
- Local activity feed and UI preference toggles.
- Instagram connect links into the existing backend `/login` flow.

## Important integration gap (do not mistake local drafts for live publishing)
The current backend has browser sign-in and Instagram OAuth routes, but does not yet expose a session-authenticated REST API for listing/uploading/deleting content-buffer assets or controlling queue/autopilot from this dashboard. The publishing service explicitly notes that web-created jobs are not yet run by the worker. For security, this frontend does **not** use the shared `WEB_API_KEY` to access user media or create cross-user publishing jobs.

Before production use, implement authenticated same-origin endpoints bound to `WebSessionStore` / `Principal` for: upload/list/delete buffer assets, queue/list/cancel/retry jobs, publishing events, and per-user automation settings; then update the frontend to call those endpoints. The worker's web publishing lifecycle and tenant ownership must be completed and tested before claiming posts are actually published.

## Deployment
- Existing static Netlify setup can publish the `web/` folder.
- `config.js` points to the current Render backend origin.
- Existing backend sign-in currently returns to its native `/web` page after OIDC; a return-to-frontend handoff should be added before treating cross-domain OAuth as a seamless user flow.
