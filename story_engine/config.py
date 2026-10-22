"""All tunable numbers/lists for the engine in one place (weights are starting points, not a proven formula)."""

# Step 3 scoring: S = 0.30V + 0.25C + 0.20R + 0.15F + 0.10D   (every term is 0-100)
WEIGHTS = {"V": 0.30, "C": 0.25, "R": 0.20, "F": 0.15, "D": 0.10}

# Safety gate
MIN_PUBLISHERS = 2              # normal trends
MIN_PUBLISHERS_SOLEMN = 3       # death / tragedy: needs stronger confirmation
MAX_AGE_HOURS = 24              # older than this is not "trending" any more
FRESH_HALF_LIFE_HOURS = 6       # freshness halves every 6h

# Publisher credibility 0-100 (lowercase). Unknown publishers get DEFAULT_CREDIBILITY. Edit to taste.
CREDIBILITY = {
    "reuters": 95, "bbc news": 92, "the hindu": 90, "espncricinfo": 90, "cricbuzz": 88,
    "ndtv": 85, "hindustan times": 85, "indian express": 86, "the indian express": 86,
    "times of india": 80, "the times of india": 80, "india today": 82, "mint": 82,
    "news18": 75, "firstpost": 72, "the economic times": 82, "bollywood hungama": 70,
    "pinkvilla": 62, "ign": 85, "polygon": 80, "variety": 88, "event_calendar": 90,
}
DEFAULT_CREDIBILITY = 50

# Audience relevance (India-first) and visual/story potential per category, 0-100
RELEVANCE = {"cricket": 100, "festival": 90, "movies": 80, "sports": 75, "gaming": 70,
             "viral": 70, "news": 60, "event": 60, "tragedy": 50}
VISUAL = {"festival": 90, "cricket": 85, "movies": 85, "gaming": 80, "sports": 75,
          "viral": 60, "event": 60, "news": 40, "tragedy": 30}
INDIA_WORDS = {"india", "indian", "bharat", "ipl", "bcci", "bollywood", "delhi", "mumbai"}
INDIA_BONUS = 10

# Words
RUMOUR_WORDS = {"rumour", "rumor", "rumours", "rumors", "unconfirmed", "reportedly", "allegedly",
                "claims", "claimed", "speculation", "fake", "hoax", "viral video claims"}
DEATH_WORDS = {"dies", "died", "death", "dead", "passes away", "passed away", "demise", "no more",
               "killed", "tragedy", "accident", "crash", "funeral", "last rites", "condolence"}
# Never auto-story these (too sensitive for automation); a human can still post them manually.
REJECT_WORDS = {"suicide", "rape", "terror attack", "terrorist", "massacre", "mass shooting",
                "sexual assault", "riot", "communal", "lynching", "bomb blast", "hostage"}

# Step 4: default (mood, angle) plans per category. AI may refine them, these are the safe fallback.
# Cricket example from the design: hype / relatable / appreciation (= achievement).
DEFAULT_ANGLES = {
    "cricket":  [("hype", "celebration"), ("hype", "relatable"), ("wholesome", "achievement")],
    "sports":   [("hype", "celebration"), ("hype", "relatable"), ("wholesome", "achievement")],
    "movies":   [("hype", "surprise"), ("funny", "relatable"), ("hype", "celebration")],
    "gaming":   [("hype", "surprise"), ("funny", "relatable"), ("hype", "celebration")],
    "festival": [("wholesome", "celebration"), ("hype", "celebration"), ("wholesome", "relatable")],
    "viral":    [("funny", "relatable"), ("funny", "surprise")],
    "news":     [("reflective", "relatable")],
    "event":    [("reflective", "relatable")],
    "tragedy":  [("solemn", "tribute")],          # only one, respectful. Never funny/hype.
}
MAX_ANGLES = 3

# Line-count guidelines per mood (min, max). Guidelines, not strict: never pad a solemn story.
LINES = {"funny": (2, 3), "hype": (2, 3), "wholesome": (2, 4), "reflective": (1, 3), "solemn": (4, 5)}

# Step 5: writer
COPY_LANGUAGE = "Hinglish in Roman script (natural Indian social-media voice, light slang, not forced)"
MAX_LINE_CHARS = 90            # hard cap per line; the layout's own max_chars is checked again in step 9
DUP_SIMILARITY = 0.6           # word-overlap above this vs a recent story = duplicate
DUP_LOOKBACK_DAYS = 7
WRITE_ATTEMPTS = 2             # retries when fact-check/duplicate check rejects
# Capitalised words the writer may use even if they are not in the headline
NAME_ALLOWLIST = {"i", "india", "indian", "indians", "bharat", "ipl", "bcci", "t20", "odi", "vibeos"}

# Step 6: visuals
import os as _os
UNSPLASH_KEY = _os.getenv("UNSPLASH_ACCESS_KEY", "")
PEXELS_KEY = _os.getenv("PEXELS_API_KEY", "")
HTTP_UA = "VibeOS-StoryEngine/1.0 (contact: vibeossupport@gmail.com)"   # Wikimedia requires a real User-Agent
# Licences we accept (lowercase, matched as prefix). Anything with NC / ND / unknown is rejected.
LICENSE_ALLOW = ("cc0", "public domain", "pd", "cc by", "cc-by", "unsplash", "pexels")
LICENSE_DENY = ("nc", "nd", "fair use", "all rights reserved", "unknown")
MIN_IMAGE_WIDTH = 1000
IMAGE_PROVIDERS = ("wikimedia", "unsplash", "pexels")        # order = priority
# Categories that must NOT get a real photo (tragedy = abstract background only; no faces next to grief text)
NO_PHOTO_CATEGORIES = {"tragedy"}
# Generic fallback backgrounds (CSS gradients, no licence issue) per mood
GENERIC_BG = {
    "hype": "linear-gradient(160deg,#ff6a00,#ee0979)", "funny": "linear-gradient(160deg,#f7971e,#ffd200)",
    "wholesome": "linear-gradient(160deg,#f6d365,#fda085)", "reflective": "linear-gradient(160deg,#304352,#d7d2cc)",
    "solemn": "linear-gradient(160deg,#232526,#414345)",
}

# Step 8: render + storage (Cloudflare R2 or S3, both via boto3)
R2_ENDPOINT = _os.getenv("R2_ENDPOINT", "")            # e.g. https://<account>.r2.cloudflarestorage.com ; empty = real AWS S3
R2_BUCKET = _os.getenv("R2_BUCKET", "")
R2_ACCESS_KEY_ID = _os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = _os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_PUBLIC_BASE = _os.getenv("R2_PUBLIC_BASE", "")      # public URL prefix of the bucket / custom domain
RENDER_SIZE = (1080, 1920)
RENDER_TIMEOUT_MS = 20000

# Step 9: quality check
QC_REQUIRE_VISION = True        # photo stories need a vision model to confirm the image fits the headline; False = skip that check
QC_BATCH = 30
