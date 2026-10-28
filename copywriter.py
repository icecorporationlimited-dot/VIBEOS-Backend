"""copywriter.py - caption + hashtag logic for VibeOS (pure functions, no network).

Why this exists
* Instagram now allows at most 5 hashtags per post/Reel (rolled out Dec 2025,
  caption + first comment combined). Old code asked for up to 10-15.
* Hashtags are now labels, not a reach hack, so we want 5 precise ones.
* The feed cuts a caption after ~125 characters, so the hook must come first.
* Captions work better when they name a real visual detail and one natural
  searchable keyword, instead of generic filler.
"""
from __future__ import annotations

import re

HASHTAG_LIMIT = 5
CAPTION_MAX_CHARS = 400          # long captions are cut by the feed anyway
HOOK_CHARS = 125                 # what the feed shows before "... more"

# --------------------------------------------------------------------------
# Caption styles (keys must stay in sync with editor.STYLES where they match)
# --------------------------------------------------------------------------
CAPTION_STYLES = {
    "baddie": "confident, bold, short, playful and stylish; one-liner energy",
    "soft": "dreamy, gentle, minimal and aesthetic; calm and warm",
    "funny": "witty, relatable, self-aware humour; a light punchline",
    "natural": "authentic, casual and simple; sounds like a real person",
    "moody": "cinematic, understated and atmospheric; a little mysterious",
    "golden": "warm, nostalgic, golden-hour feeling; grateful and glowing",
    "clean": "fresh, minimal, polished; short and confident",
    "film": "nostalgic, analog, diary-like; quiet and observant",
}

# Used only when the AI caption service is unreachable on both attempts:
# a neutral, honest one-liner keeps the photo flowing instead of failing the
# whole prepare. No hashtags (better none than wrong ones).
FALLBACK_CAPTIONS = {
    "baddie": "unbothered, just letting the light talk",
    "soft": "soft light, slow day",
    "funny": "no thoughts, just this view",
    "natural": "keeping this one",
    "moody": "the light did all the talking today.",
    "golden": "golden hour never misses",
    "clean": "simple, quiet, exactly right.",
    "film": "shot on a random tuesday",
}

LANGUAGES = {
    "english": "natural, modern English",
    "hinglish": (
        "natural Hinglish: Hindi + English mixed, written in Roman script "
        "(like a young Indian creator talks). Not textbook Hindi, no Devanagari."
    ),
}

# Tags that are too generic / spammy to help classification or that look
# like engagement bait. Matched on the lowercase tag without '#'.
GENERIC_BLOCKLIST = {
    "love", "instagood", "photooftheday", "fashion", "beautiful", "happy",
    "cute", "like", "likeforlike", "like4like", "follow", "followme",
    "follow4follow", "f4f", "l4l", "instalike", "instadaily", "picoftheday",
    "photography", "fyp", "foryou", "foryoupage", "viral", "trending",
    "explore", "explorepage", "reels", "reel", "instagram", "insta", "tbt",
    "followforfollow", "likes", "comment", "share", "bestoftheday", "me",
    "selfie", "style", "vibes", "vibe", "mood", "aesthetic", "life",
}

_TAG_BODY = re.compile(r"[^\w]", re.UNICODE)


def _tokens(raw) -> list[str]:
    """Plain string -> split on spaces/commas. List -> one tag per item
    (so "#Street Style" becomes #streetstyle), unless an item holds several
    '#tags' in one string."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [t for t in re.split(r"[\s,]+", raw) if t]
    if isinstance(raw, (list, tuple, set)):
        out: list[str] = []
        for item in raw:
            item = str(item)
            if item.count("#") > 1:
                out.extend(t for t in item.split("#") if t.strip())
            elif item.strip():
                out.append(item)
        return out
    return []


def sanitize_hashtags(raw, limit: int = HASHTAG_LIMIT, *, drop_generic: bool = True,
                      avoid_words: str = "") -> list[str]:
    """Return at most `limit` clean, unique, lowercase '#tags'.

    * accepts a list or a free-text string ("#a #b, c")
    * removes punctuation/spaces, pure numbers, too-short/too-long tags
    * drops spammy generic tags (unless drop_generic=False)
    * dedupes case-insensitively and keeps the model's order (it is asked to
      put the most specific tag first)
    * `avoid_words`: tags that only repeat a word already in the caption add
      nothing, so a tag equal to a caption word is moved after the others
    """
    seen: set[str] = set()
    fresh: list[str] = []
    repeats: list[str] = []
    caption_words = set(re.findall(r"\w+", (avoid_words or "").lower()))
    for tok in _tokens(raw):
        body = _TAG_BODY.sub("", tok.lstrip("#")).lower()
        if not (3 <= len(body) <= 30) or body.isdigit():
            continue
        if drop_generic and body in GENERIC_BLOCKLIST:
            continue
        if body in seen:
            continue
        seen.add(body)
        (repeats if body in caption_words else fresh).append("#" + body)
    return (fresh + repeats)[: max(0, int(limit))]


def clean_caption(text, max_chars: int = CAPTION_MAX_CHARS) -> str:
    """Tidy model output: no hashtags, no wrapping quotes, tidy whitespace."""
    text = str(text or "").replace("\r", "")
    text = re.sub(r"(?<!\w)#\w+", "", text)               # hashtags belong elsewhere
    text = text.strip().strip("\"'\u201c\u201d").strip()
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        # prefer ending on a sentence boundary
        m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
        text = cut[: m + 1].strip() if m >= int(max_chars * 0.5) else cut.rstrip()
    return text


def hook_of(caption: str) -> str:
    """The part of the caption the feed shows before 'more'."""
    return (caption or "")[:HOOK_CHARS]


def build_prompt(*, mode: str, vibe: str, language: str = "english",
                 trend_context: str | None = None,
                 recent_captions: list[str] | None = None,
                 overlay_required: bool = False) -> str:
    style = CAPTION_STYLES.get(mode) or CAPTION_STYLES.get(vibe) or CAPTION_STYLES["natural"]
    lang = LANGUAGES.get(language, LANGUAGES["english"])
    recent = [c.strip().replace("\n", " ")[:140] for c in (recent_captions or []) if c and c.strip()][:8]
    recent_block = (
        "Captions already used recently (do NOT reuse their wording, opening words "
        "or structure):\n" + "\n".join(f"- {c}" for c in recent)
        if recent else "No recent captions to avoid."
    )
    overlay_rule = (
        "Overlay rules: text on the photo is REQUIRED for this post. A punchy 3-6 word line, lowercase, "
        "no emoji, no hashtags, no quotation marks, in the same language as the caption."
        if overlay_required else
        'Overlay rules: do NOT put text on the photo. Always return "overlay": "" (an empty string).'
    )
    return f"""You are the caption writer for a top Instagram creator. You write like a real person, never like an ad.

Caption style: {mode} -> {style}
Photo vibe: {vibe}
Language: {lang}

Trend inspiration (optional):
{trend_context or "none"}
{recent_block}

Work in this order:
1. Look closely at the photo and note 3-4 concrete visual details (outfit colours, setting, light, mood, objects, time of day). Do not guess names, places, events, relationships or facts you cannot see. Do not identify the person.
2. Write the caption using ONE of those details, so it could only belong to this photo.
3. Pick the hashtags.

Caption rules:
- The first line is the hook and must work alone in under 90 characters, because the feed cuts the caption after ~125 characters.
- Total length: 1-3 short lines, at most 220 characters.
- Include 1-2 natural, searchable keywords for what is in the photo (for example "golden hour portrait", "street style", "cafe corner"). Instagram search reads caption text.
- At most 2 emoji, only if they fit. No hashtags in the caption. No quotation marks around it.
- Banned filler: "living my best life", "good vibes only", "slay", "obsessed", "main character" (unless truly funny), "just a girl", "Monday mood" and similar clich\u00e9s.
- No body comparisons, no appearance judgements, no sensitive-trait guesses.
- Do not claim any song or trend is popular unless the trend inspiration above says so.
- Sometimes (about 1 in 3) end with a very light question or save/share nudge. Never "like and follow".

Hashtag rules (Instagram allows MAX 5, so each one must earn its place):
- Return exactly 5, most specific first, in this order of roles: (1) what is literally in the photo, (2) the setting or occasion ONLY if clearly visible, (3) a niche community tag real people browse, (4) the look/style, (5) one broader topic tag.
- Lowercase letters and numbers only. No generic spam like #love #instagood #fyp #viral #photooftheday #explore #follow.
- No tag that only repeats a caption word without adding meaning. Never invent events, festivals or locations.

{overlay_rule}

Return ONLY valid JSON, nothing else:
{{
  "visual_notes": "3-4 short concrete details you noticed",
  "caption": "the caption",
  "overlay": "",
  "hashtags": ["#one", "#two", "#three", "#four", "#five"]
}}"""


def parse_copy(data: dict, *, mode: str) -> dict:
    """Validate/clean the model's JSON into the shape the bot stores."""
    caption = clean_caption(data.get("caption", ""))
    if not caption:
        raise ValueError("AI returned an empty caption")
    tags = sanitize_hashtags(data.get("hashtags", []), avoid_words=caption)
    return {"caption": caption, "hashtags": tags, "caption_style": mode}
