# Deploy VIBEOS frontend to Netlify

1. In the GitHub repository connected to Netlify, replace the contents of its published frontend directory with the contents of this `web/` directory.
2. If Netlify's **Base directory** is `web`, set **Publish directory** to `.`. If the repository is frontend-only and these files are at its root, set **Publish directory** to `.` and leave Base directory blank.
3. Trigger a deploy and hard-refresh the site.

The dashboard works on Netlify without running Python locally. Browser uploads, queue plans, activity and preferences are saved in IndexedDB in that browser. They do not sync across devices.

Instagram Connect opens the existing Render login route. The queue is a planner only: actual scheduled Instagram publishing is not enabled by this static frontend. That requires the authenticated Render APIs and publishing worker integration.
