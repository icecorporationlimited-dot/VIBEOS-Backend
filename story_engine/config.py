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
    # ── Global news & wire agencies ──
    "reuters": 97, "associated press": 96, "ap news": 96, "afp": 95, "bbc news": 94,
    "the guardian": 91, "the new york times": 91, "the washington post": 90, "al jazeera": 89,
    "bloomberg": 91, "the wall street journal": 90, "cnn": 86, "sky news": 87, "dw": 88,
    "france 24": 87, "the independent": 85, "nbc news": 87, "abc news": 87, "cbs news": 86,
    "the times": 85,

    # ── Indian national news ──
    "the hindu": 92, "the indian express": 92, "indian express": 92, "ndtv": 87,
    "hindustan times": 87, "times of india": 84, "the times of india": 84, "india today": 85,
    "mint": 88, "the economic times": 87, "business standard": 87, "the print": 84,
    "scroll.in": 85, "the wire": 82, "deccan herald": 85, "the telegraph india": 84,
    "news18": 80, "india tv": 70, "aaj tak": 79, "abp news": 79, "zee news": 70,
    "firstpost": 72, "the quint": 81, "moneycontrol": 84, "pti": 93, "ani": 86,

    # ── Official & government sources ──
    "pib": 96, "press information bureau": 96, "mygov": 94, "india.gov.in": 95, "eci": 98,
    "election commission of india": 98, "imd": 98, "india meteorological department": 98,
    "ndma": 97, "national disaster management authority": 97, "isro": 98,
    "ministry of home affairs": 97, "ministry of health and family welfare": 97,
    "ministry of external affairs": 97, "supreme court of india": 98, "rbi": 98, "nasa": 98,
    "who": 97, "un": 95, "unicef": 95,

    # ── Cricket & sports ──
    "espncricinfo": 96, "cricbuzz": 93, "icc": 98, "bcci": 98, "fifa": 98, "olympics.com": 97,
    "olympic committee": 97, "the athletic": 89, "bbc sport": 91, "sky sports": 90,
    "sportstar": 88, "sportskeeda": 70, "goal.com": 83, "espn": 91, "nba.com": 97,

    # ── Gaming & esports ──
    "ign": 88, "polygon": 85, "gamespot": 85, "pc gamer": 85, "eurogamer": 86, "the verge": 87,
    "kotaku": 80, "rock paper shotgun": 84, "dexerto": 78, "esports insider": 82,
    "liquipedia": 88, "steam": 98, "playstation blog": 96, "xbox wire": 96, "nintendo": 97,
    "riot games": 97, "valve": 97, "pubg esports": 96, "esports world cup": 95,

    # ── Movies, TV & entertainment ──
    "variety": 91, "hollywood reporter": 90, "deadline": 89, "the hollywood reporter": 90,
    "bollywood hungama": 78, "pinkvilla": 73, "film companion": 82,
    "indian express entertainment": 87, "peeping moon": 70, "koimoi": 70,
    "box office india": 80, "imdb": 70, "rottentomatoes": 70, "letterboxd": 65,
    "netflix": 98, "prime video": 97, "disney+": 97, "jiohotstar": 97,
    "amazon mgm studios": 96, "warner bros": 96, "marvel": 97, "dc": 95,
    "yash raj films": 96, "dharma productions": 95, "t-series": 95,

    # ── Festivals, holidays & occasions ──
    "event_calendar": 90, "timeanddate": 90, "drik panchang": 90, "india.gov.in calendar": 95,
    "pib festival announcement": 96, "incredible india": 93, "un tourism": 93, "un days": 96,
    "official festival website": 96, "state tourism department": 94, "state government": 95,
    "local municipal corporation": 94,

    # ── Viral trends & social media ──
    "google trends": 92, "youtube trends": 90, "youtube official": 96, "instagram": 70,
    "instagram official account": 92, "x": 70, "x verified official account": 92,
    "reddit": 60, "tiktok": 65, "meta newsroom": 97, "youtube blog": 95,
    "trend discovery engine": 70,

    # ── Fact-checking ──
    "alt news": 90, "boom live": 90, "factly": 89, "vishvas news": 87, "afp fact check": 94,
    "reuters fact check": 96, "ap fact check": 95, "snopes": 90, "politifact": 90,
    "full fact": 90,

    # ── Science, health & weather ──
    "nature": 96, "science": 96, "scientific american": 90, "lancet": 96, "pubmed": 95,
    "cdc": 96, "imd weather alerts": 98, "usgs": 97, "noaa": 97, "esa": 97,

    # ── Tragedies, accidents & emergencies ──
    "local police official": 97, "district administration": 97, "state disaster authority": 97,
    "fire department official": 96, "official hospital statement": 95,
    "official airline statement": 95, "railway ministry": 97, "indian railways": 97,
    "national transport safety authority": 97,

    # ── Business & technology ──
    "techcrunch": 85, "wired": 87, "arstechnica": 88, "cnbc": 87, "forbes": 82, "fortune": 84,
    "yahoo finance": 83, "financial times": 92, "the information": 86,
    "official company newsroom": 96,
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

# ---------------------------------------------------------------------------------------------
# Step 2: discovery sources.  TREND sources only; image sources live in step 6 (visuals.py).
# A source finds candidate events. It never decides truth: step 3 (verify.py) still needs
# MIN_PUBLISHERS independent publishers, so one official blog alone stays on "hold".
# Feed URLs are config, not code: run `python -m story_engine.discovery.check` to see which are alive,
# then edit/disable here. A dead feed is skipped and logged; it never stops the pipeline.
# ---------------------------------------------------------------------------------------------
DISCOVERY_ENABLED = {"news_rss": True, "feeds": True, "search_trends": True, "gnews": True, "festival": True}

FEED_MAX_ITEMS = 15            # newest items kept per feed
FEED_TIMEOUT = 12              # seconds
FEED_MAX_BYTES = 2_000_000     # bigger responses are rejected
FEED_MIN_TITLE = 15            # shorter titles are navigation/junk, not headlines

# category: cricket|sports|movies|gaming|news. official=True means the publisher is the event owner
# (a game publisher's own blog); it is only a hint, it does NOT bypass the publisher-count gate.
DISCOVERY_FEEDS = [
    {"name": "ESPNcricinfo", "category": "cricket", "official": False,
     "url": "https://www.espncricinfo.com/rss/content/story/feeds/0.xml"},
    {"name": "The Hindu", "category": "cricket", "official": False,
     "url": "https://www.thehindu.com/sport/cricket/feeder/default.rss"},
    {"name": "Hindustan Times", "category": "cricket", "official": False,
     "url": "https://www.hindustantimes.com/feeds/rss/cricket/rssfeed.xml"},
    {"name": "Indian Express", "category": "cricket", "official": False,
     "url": "https://indianexpress.com/section/sports/cricket/feed/"},
    {"name": "Times of India", "category": "cricket", "official": False,
     "url": "https://timesofindia.indiatimes.com/rssfeeds/4719161.cms"},
    {"name": "NDTV", "category": "cricket", "official": False,
     "url": "https://feeds.feedburner.com/ndtvsports-cricket"},
    {"name": "The Hindu", "category": "sports", "official": False,
     "url": "https://www.thehindu.com/sport/feeder/default.rss"},
    {"name": "Hindustan Times", "category": "sports", "official": False,
     "url": "https://www.hindustantimes.com/feeds/rss/sports/rssfeed.xml"},
    {"name": "Indian Express", "category": "sports", "official": False,
     "url": "https://indianexpress.com/section/sports/feed/"},
    {"name": "Variety", "category": "movies", "official": False, "url": "https://variety.com/feed/"},
    {"name": "Bollywood Hungama", "category": "movies", "official": False,
     "url": "https://www.bollywoodhungama.com/rss/news.xml"},
    {"name": "Hindustan Times", "category": "movies", "official": False,
     "url": "https://www.hindustantimes.com/feeds/rss/entertainment/bollywood/rssfeed.xml"},
    {"name": "Indian Express", "category": "movies", "official": False,
     "url": "https://indianexpress.com/section/entertainment/bollywood/feed/"},
    {"name": "IGN", "category": "gaming", "official": False, "url": "https://feeds.feedburner.com/ign/all"},
    {"name": "Polygon", "category": "gaming", "official": False, "url": "https://www.polygon.com/rss/index.xml"},
    {"name": "PlayStation Blog", "category": "gaming", "official": True, "url": "https://blog.playstation.com/feed/"},
    {"name": "Xbox Wire", "category": "gaming", "official": True, "url": "https://news.xbox.com/en-us/feed/"},
    # Official league / team / studio feeds: add them here, e.g.
    # {"name": "ICC", "category": "cricket", "official": True, "url": "<official feed url>"},
]
for _f in DISCOVERY_FEEDS:
    _f.setdefault("enabled", True)

# Search trends (Google Trends trending-searches RSS). Only trends that carry real news headlines are used.
SEARCH_TRENDS_REGION = "IN"
SEARCH_TRENDS_MAX = 20
SEARCH_MATCH = 0.35            # a news item must resemble the chosen headline this much to count as a source

# GNews official API (optional, needs GNEWS_API_KEY). Free plan ~100 requests/day, shared with trends/ enrich.
GNEWS_KEY = _os.getenv("GNEWS_API_KEY", "")
GNEWS_QUERIES = {"cricket": "india cricket", "sports": "india sports", "movies": "bollywood",
                 "gaming": "video game release", "viral": "viral india", "news": "india"}
GNEWS_MIN_INTERVAL_MIN = 180   # 6 queries / 3h = 48 requests a day

# Categories that are only a fallback: a specific category wins when the same event is seen as both.
GENERIC_CATEGORIES = {"news", "viral", "event"}

# Headline -> category when a source has no category of its own (search trends, general news).
# Order = tie-break priority. Whole-word / whole-phrase match, lowercase.
CLASSIFY_KEYWORDS = {
    "cricket": ["cricket", "ipl", "odi", "t20", "t20i", "bcci", "icc", "wicket", "wickets", "innings", "batter",
                "bowler", "test match", "ranji", "asia cup", "world cup final", "kohli", "rohit sharma", "dhoni",
                "bumrah"],
    "sports": ["football", "fifa", "nba", "tennis", "wimbledon", "olympic", "olympics", "hockey", "kabaddi",
               "formula 1", "f1", "badminton", "isl", "premier league", "grand slam", "athletics"],
    "gaming": ["gaming", "playstation", "xbox", "nintendo", "steam", "gta", "esports", "bgmi", "valorant",
               "video game", "ps5", "console"],
    "movies": ["movie", "film", "trailer", "teaser", "box office", "bollywood", "tollywood", "ott", "web series",
               "netflix", "release date", "blockbuster", "actor", "actress", "director"],
    "festival": ["diwali", "holi", "eid", "christmas", "navratri", "dussehra", "raksha bandhan", "ganesh chaturthi",
                 "durga puja", "onam", "pongal", "baisakhi"],
    "viral": ["viral", "meme", "trolled", "internet reacts", "goes viral"],
}

# Same publisher, different spellings across sources -> ONE name, so it can never count as two publishers.
PUBLISHER_ALIASES = {
    "the indian express": "Indian Express", "indian express": "Indian Express",
    "the times of india": "Times of India", "times of india": "Times of India", "toi": "Times of India",
    "the economic times": "The Economic Times", "economic times": "The Economic Times",
    "ndtv sports": "NDTV", "ndtv.com": "NDTV", "ndtv": "NDTV",
    "espn cricinfo": "ESPNcricinfo", "espncricinfo": "ESPNcricinfo", "espncricinfo.com": "ESPNcricinfo",
    "hindustan times": "Hindustan Times", "hindustantimes.com": "Hindustan Times",
    "sportstar": "The Hindu", "the hindu": "The Hindu", "thehindu.com": "The Hindu",
    "cnn-news18": "News18", "news18": "News18", "ign india": "IGN",
}

