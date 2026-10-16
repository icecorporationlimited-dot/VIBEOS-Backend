# VIBEOS Autonomous Content Engine — first implementation

This patch adds a conservative decision layer, a real photo-slideshow Reel renderer,
and a Google Trends → branded Story-draft path. It is a first working layer, not a
claim that every Instagram action is now autonomous.

## Added modules

- `autonomous/decision_engine.py`: routes assets to photo/story/carousel and gates trend candidates. It never labels an ordinary photo as a festival post without a high-confidence event label.
- `creative_studio/reel_engine.py`: `render_photo_reel(...)` makes a vertical H.264 slideshow MP4 from photos with FFmpeg. Optional audio must be supplied by the caller and rights-cleared; no song is downloaded automatically.
- `creative_studio/story_templates.py`: renders a branded 1080×1920 Story draft with the trend title and source label.
- `trends/sources/google_trends.py`: reads Google's public trending-search RSS feed for a region; feed data is a search signal, not verified news.
- `trends/story_pipeline.py`: creates local Story drafts only. It deliberately does not auto-publish a current-affairs claim.

## Quick use

Run from the `vibeos/` project directory.

```python
from autonomous.decision_engine import decide_content

choice = decide_content(
    analysis={"quality_score": 0.82},
    event_detection={
        "event_related": False,
        "event_key": "",
        "content_type": "general",
        "confidence": 0.12,
    },
    vibe="baddie",
)
print(choice)  # format=photo, content_source=personal_photo
```

Render a Reel from local photos (FFmpeg must be installed):

```python
from creative_studio.reel_engine import render_photo_reel
render_photo_reel(
    ["media/one.jpg", "media/two.jpg", "media/three.jpg"],
    "output/reel.mp4",
    seconds_per_photo=2.5,
)
```

Generate local trend Story drafts (internet access is required):

```python
from trends.story_pipeline import create_story_drafts

drafts = create_story_drafts(
    "output/trend_stories",
    region="IN",
    limit=3,
    brand="VIBEOS",
    brand_topics=["fashion", "music", "creator", "entertainment"],
    blocked_terms=["unverified allegation"],
)
print(drafts)
```

## Integration points

1. Call `decide_content(...)` after image/occasion analysis and before creating a publishing job.
2. If it returns `format="carousel"`, pass 2–10 selected distinct asset IDs to the existing publisher.
3. For a Reel, first call `render_photo_reel(...)`, then upload the MP4 through a separately implemented/eligible video-publishing path. The current publisher's accepted formats are photo/story/carousel; this patch does **not** pretend its current API can publish Reels.
4. `create_story_drafts(...)` produces local JPG drafts only. Fact-check the linked source and wire publishing through the existing Instagram API only after the story publishing endpoint, account eligibility, permissions, and tests are confirmed.
5. Comments, replies, profile-picture and bio changes are not implemented by this patch. These need their own Meta permission/endpoint checks and conservative policies.

## Requirements and limitations

- Python packages already in `requirements.txt` include Pillow and requests.
- Install FFmpeg/ffprobe on the host for Reel rendering. Android/Pydroid and some free hosts may not have FFmpeg available.
- Google Trends RSS and YouTube results are not Instagram trend data. Never state a song is trending on Instagram or licensed merely because YouTube lists it.
- The Story template intentionally says “Trend signal — details are being verified.” It should not be published as a factual news claim without verification.
- Do not run this against a public account until the owner has tested it with a private/test account and understands which actions publish automatically.
