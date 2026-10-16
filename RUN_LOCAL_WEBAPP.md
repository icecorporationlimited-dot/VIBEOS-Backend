# VIBEOS Creator Studio — Local Run

## Start

- **Windows:** double-click `RUN_VIBEOS_WINDOWS.bat`
- **Android/Termux, Linux or macOS:** run `python3 run_webapp.py`
- Open `http://127.0.0.1:8765` if the browser does not open automatically.

Python 3.10+ is recommended. No third-party Python packages are required for local dashboard mode.

## What works locally

- Upload JPG/JPEG/PNG/WEBP/GIF photos (up to 10 MB each) to persistent local disk storage.
- Browse and delete uploaded photos; deleting media also removes queue plans that reference it.
- Create, edit, schedule and remove queue plans; data is stored in SQLite.
- Activity notifications for uploads, queue changes, deletions and settings changes.
- Persistent Autopilot, Auto Story and Auto captions preferences.
- Data survives browser refreshes and restarts. Data folder: `vibeos_local_data/`.

## Important Instagram publishing limitation

This is a runnable local Creator Studio, but it does **not** claim that a queued item has been published. The existing repository's publishing worker explicitly keeps browser-created jobs out of its live publishing lifecycle until hosted web publishing is integrated. Local queue items are plans only; there are no fake “published” notifications.

The Instagram connect link points to the configured hosted VIBEOS login by default (`https://vibeos-backend.onrender.com/login`). Set `VIBEOS_INSTAGRAM_CONNECT_URL` to override the link. Linking a hosted account does not automatically connect this local SQLite queue to the hosted worker.

To activate real Instagram publishing, the hosted backend must have valid Meta app credentials, eligible Instagram professional account permissions, and a secure session-scoped web queue/worker integration. Do not expose a Meta token or `WEB_API_KEY` in frontend JavaScript.

## Local data reset

Use **Settings → Reset browser workspace** to remove stored photos and queue plans. Or stop the server and delete `vibeos_local_data/`.
