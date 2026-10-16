# VIBEOS Creator Studio — Netlify frontend

The static frontend runs on Netlify without `run_webapp.py`. Media, queue drafts, activity notifications, and settings are stored in the current browser using IndexedDB. They persist across refreshes in that browser, but are not shared with other devices or the Render backend.

## Functional in browser workspace
- Upload image files, preview, and delete them.
- Create/edit/remove Story, Post, and Reel queue plans, with optional scheduled time.
- View and clear activity notifications.
- Save Autopilot, Auto Story, and Auto Captions preferences.
- Open the existing hosted Instagram login URL.

## Not yet live publishing
Queue entries are drafts/plans only. Browser preferences do not activate Render automation. Publishing to Instagram and server-side notifications require authenticated backend endpoints, persistent hosted media storage, and integration with the publishing worker/Meta permissions. Do not display a queue plan as a published post.
