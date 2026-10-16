# Caption, hashtag and editor upgrade

## Captions and hashtags (`copywriter.py`)
- Instagram allows **max 5 hashtags** per post/Reel (since Dec 2025, caption + first
  comment combined). Everything is capped at 5: AI output, stored tags, publishing
  (`HASHTAG_LIMIT = 5`), Reel tag merging.
- New prompt: the model first notes real visual details, writes a hook (<90 chars,
  the feed cuts at ~125), adds 1-2 searchable keywords, avoids clichés, and avoids
  the user's last 8 captions.
- Exactly 5 hashtags with roles: subject, setting (only if visible), niche community,
  look/style, one broad tag. Generic spam (#love #fyp #instagood ...) is removed.
- 8 caption styles: baddie soft funny natural moody golden clean film.
- Hinglish: `/caption baddie hinglish`, or set `CAPTION_LANGUAGE = "hinglish"` in
  settings / settings_local.py for a default.
- One automatic retry if the model returns broken JSON.

## Editor (`editor.py`)
Auto-fix first, then the style:
- Subject-aware crop (face detection if OpenCV is installed, otherwise a built-in
  skin/edge saliency map). Heads are not cut in 4:5 and 9:16 crops.
- White balance (max +-7%), adaptive exposure (linear light, soft highlight
  shoulder), shadow lift, highlight recovery, denoise for noisy night photos.
- Skin-safe vibrance, clarity (local contrast), resolution-aware sharpening.
- New looks: `golden`, `clean`, `film` (use `/vibe golden` etc.). `moody` keeps its
  darkness (lower auto-exposure target).
- `edit_photo(..., return_report=True)` returns what the auto-fix did.

## Optional: better face detection
`pip install -r requirements-optional.txt` (opencv-python-headless). Detected
automatically; without it the built-in fallback is used.
