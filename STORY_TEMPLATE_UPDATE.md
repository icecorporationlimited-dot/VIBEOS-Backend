# VibeOS Story Template Update

## What's changed

- Added eight active 1080x1920 story layouts: `genz_text_01`, `celebrity_reaction_01`, `meme_reaction_01`, `sports_fan_01`, `movie_hype_01`, `tribute_01`, `moody_late_01`, and `trend_remix_01`.
- Each layout has its own CSS composition, typography, alignment, gradient treatment, and text rhythm. Templates use the existing `{{BG}}`, `{{LINES}}`, and `{{CREDIT}}` placeholders so the existing Playwright renderer remains compatible.
- Old template folders and manifests are retained but marked inactive. This preserves rendering support for already-assigned assets while preventing the old layouts from being selected for new stories.
- `story_engine/layouts.py` now syncs active/inactive manifests into MongoDB and chooses between eligible standard and special layouts with a 70/30 target. If a special layout does not fit the category/mood (especially solemn/tribute content), it falls back to an eligible standard layout.
- Story source/credit labels are not added as editorial UI. The existing credit slot only receives text when an image's licence explicitly requires attribution.

## Deployment

1. Replace the backend files with this ZIP's contents and deploy as usual.
2. Ensure the existing startup path calls `story_engine.layouts.sync(db)` (the current `run_loop.py` and `web.py` already do).
3. New pending assets will use the eight active layouts. Existing assets already assigned a legacy layout can still render because their original template folders remain present.

## Notes

- The 70/30 split is a probabilistic target among eligible layouts, not a strict guarantee for every small batch. Special layouts are curated flexible compositions selected alongside AI-written copy; this change does not add an LLM that writes arbitrary new HTML/CSS at runtime.
- Python compile check passed. The repository's full pytest run currently has 34 passing tests and 2 unrelated failures because `tests/test_jobs.py` imports `trends.auto_story`, which is absent from this ZIP's `trends` package.
