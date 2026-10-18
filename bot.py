import os
import io
import re
import json
import base64
import asyncio
import uuid
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pymongo
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageDraw
from groq import Groq

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

import settings
import copywriter
import imgutil
import jobs
from media_names import STYLES as EDITOR_STYLES   # names only; editor.py runs in worker.py
from content_buffer import ContentBuffer, GridFSBlobStore
import memory
import publish_bot
from ratelimit import make_gate
from publishing.autopilot import AutopilotStore

# =========================================================
# CONFIGURATION
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BOT_TOKEN = settings.BOT_TOKEN
MONGODB_URI = settings.MONGODB_URI
GROQ_API_KEY = settings.GROQ_API_KEY

_missing = [k for k, v in {
    "BOT_TOKEN": BOT_TOKEN,
    "MONGODB_URI": MONGODB_URI,
    "GROQ_API_KEY": GROQ_API_KEY,
}.items() if not v or str(v).startswith("your_")]
if _missing:
    raise SystemExit(f"Fill these in settings.py: {', '.join(_missing)}")

MODEL = settings.VISION_MODEL
DEFAULT_VIBE = memory.DEFAULT_STYLE

MAX_PHOTO_SIZE = 10 * 1024 * 1024
MAX_ALBUM_PHOTOS = 10

# One shared Mongo connection (owned by memory.py).
mongo_client = memory.client
db = memory.db

users = db["users"]
photos = db["photos"]
batches = db["batches"]
grid_entries = db["grid_entries"]
trends = db["trends"]

grid_entries.create_index(
    [("telegram_id", 1), ("photo_id", 1)],
    unique=True,
)

groq_client = Groq(api_key=GROQ_API_KEY)

# Phase 1: durable media buffer (GridFS blobs + asset status in MongoDB)
content_buffer = ContentBuffer(db["assets"], GridFSBlobStore(db))
SHORTLIST_SIZE = 3
MIN_AUTO_SCORE = 55   # full-auto only publishes photos the AI scores at least this
_autopilot = AutopilotStore(db["autopilot"])


def full_auto_on(user_id) -> bool:
    try:
        return bool(_autopilot.get(user_id).get("full_auto"))
    except Exception:
        logger.exception("Could not read autopilot settings")
        return False

photo_batches = {}


# =========================================================
# GENERAL HELPERS
# =========================================================

def now_utc():
    return datetime.now(timezone.utc)


def get_user_vibe(user_id):
    return memory.get_preferences(user_id)["vibe"]


def image_to_base64(image_bytes):
    return base64.b64encode(image_bytes).decode("utf-8")


def parse_ai_json(text):
    # Reasoning models can prepend <think> blocks; strip them first.
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    text = text.strip()

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end < start:
        raise ValueError("No JSON object found in AI response")

    return json.loads(text[start:end + 1])


def clamp_score(value):
    try:
        return max(0, min(100, int(float(value))))
    except (ValueError, TypeError):
        return 0


def clean_hashtags(value):
    """Instagram allows max 5 hashtags per post, so never keep more."""
    return copywriter.sanitize_hashtags(
        value, limit=copywriter.HASHTAG_LIMIT, drop_generic=False
    )


async def download_telegram_photo(context, file_id):
    telegram_file = await context.bot.get_file(file_id)
    buffer = io.BytesIO()

    await telegram_file.download_to_memory(buffer)

    return imgutil.downscale(buffer.getvalue())   # max 2048 px long edge


EDIT_JOB_TIMEOUT = 120


async def edit_via_worker(data, style, size="post", text=None,
                          text_style="minimal", text_position="bottom"):
    """Drop-in for editor.edit_photo(): the edit runs in the worker service.

    Enqueues an `edit` job, waits for it without blocking the event loop and
    returns a BytesIO JPEG, so callers stay unchanged. Raises jobs.JobFailed /
    jobs.JobTimeout if the worker is down or the job keeps failing.
    """
    blob_id = await asyncio.to_thread(jobs.put_blob, db, data, "edit_in.jpg")
    res = await jobs.run_async(db, "edit", {
        "in_blob": blob_id, "style": style, "size": size, "text": text,
        "text_style": text_style, "text_position": text_position,
    }, timeout=EDIT_JOB_TIMEOUT)
    out = await asyncio.to_thread(jobs.get_blob, db, res["blob_id"])
    await asyncio.to_thread(jobs.delete_blob, db, res["blob_id"])
    return io.BytesIO(out)


def get_latest_owned_photo(user_id, include_approved=True):
    statuses = ["preview_ready"]

    if include_approved:
        statuses.append("approved")

    return photos.find_one(
        {
            "telegram_id": str(user_id),
            "status": {"$in": statuses},
        },
        sort=[("created_at", pymongo.DESCENDING)],
    )


TEXT_STYLES = ["minimal", "bold", "serif"]


def clean_overlay(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'")
    text = "".join(ch for ch in text if ord(ch) < 0x2070)
    return " ".join(text.split()[:7])[:60]


def render_settings(doc):
    return {
        "size": doc.get("render_size", "post"),
        "text_style": doc.get("text_style", "minimal"),
        "show_text": doc.get("show_text", True),
    }


def preview_keyboard(photo_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📱 Story / 📷 Post",
                callback_data=f"story:{photo_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔤 Text Style",
                callback_data=f"tstyle:{photo_id}",
            ),
            InlineKeyboardButton(
                "👁 Text On/Off",
                callback_data=f"ttoggle:{photo_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 Regenerate Caption",
                callback_data=f"regencap:{photo_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 Hashtags",
                callback_data=f"hashtags:{photo_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "✅ APPROVE",
                callback_data=f"approve:{photo_id}",
            ),
            InlineKeyboardButton(
                "❌ DISCARD",
                callback_data=f"discard:{photo_id}",
            ),
        ],
    ])


def format_preview_caption(doc):
    analysis = doc.get("analysis", {})
    vibe = doc.get("vibe", DEFAULT_VIBE)
    style = analysis.get("filter", vibe)

    caption = str(doc.get("caption", "Just vibes ✨"))
    hashtags = clean_hashtags(doc.get("hashtags", []))

    text = (
        "✨ VibeOS Preview\n\n"
        f"🎨 Vibe: {vibe}\n"
        f"🪄 Filter: {style}\n"
        f"📐 {'Story 9:16' if doc.get('render_size') == 'story' else 'Post 4:5'}"
        f" | 🔤 {doc.get('text_style', 'minimal')}"
        f"{'' if doc.get('show_text', True) else ' (off)'}\n\n"
        f"💬 {caption}\n\n"
        f"{' '.join(hashtags)}\n\n"
        "Regenerate your caption, edit hashtags, or approve."
    )

    return text[:1024]


# =========================================================
# TREND BRAIN  (Phase 4 — real sources only, no AI-written ideas)
# =========================================================

from trends import store as trend_store
from trends import relevance as trend_rel


def load_trend_picks(user_id, limit=5):
    """Active, gated, real-source trends for this user: {'audio': [...], 'hashtag': [...]}.

    Returns None when nothing usable exists.
    """
    try:
        items = trend_store.active_items(db)
        vibe = memory.get_preferences(user_id)["vibe"]
        try:
            from publishing.autopilot import AutopilotStore
            blocked = AutopilotStore(db["autopilot"]).get(user_id).get("blocked_topics", [])
        except Exception:
            blocked = []
        picks = trend_rel.pick(items, vibe, blocked_terms=blocked, limit=limit)
    except Exception:
        logger.exception("load_trend_picks failed")
        return None
    return picks if (picks["audio"] or picks["hashtag"]) else None


def _ago(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    hours = int((datetime.now(timezone.utc) - dt).total_seconds() // 3600)
    return "just now" if hours < 1 else f"{hours}h ago"


def load_latest_trends():
    """Load today's trend document, supporting old and new formats."""
    today = datetime.now(
        ZoneInfo("Asia/Kolkata")
    ).strftime("%Y-%m-%d")

    doc = trends.find_one({"date": today})

    if not doc:
        doc = trends.find_one(
            {},
            sort=[("created_at", pymongo.DESCENDING)],
        )

    if not doc:
        return None

    data = doc.get("data", {})

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Saved trend data is invalid JSON")
            return None

    if not isinstance(data, dict):
        return None

    audios = data.get("audios", [])
    captions = data.get("captions", [])

    if not isinstance(audios, list):
        audios = []

    if not isinstance(captions, list):
        captions = []

    return {
        "date": doc.get("date", "Unknown"),
        "audios": audios,
        "captions": captions,
    }


def format_audio_suggestion(audio):
    if isinstance(audio, str):
        return audio

    if isinstance(audio, dict):
        name = str(audio.get("name", "Audio idea")).strip()
        vibe = str(audio.get("vibe", "")).strip()
        use_case = str(audio.get("use_case", "")).strip()

        result = f"🎵 {name}"

        if vibe:
            result += f"\n   Vibe: {vibe}"

        if use_case:
            result += f"\n   Use it for: {use_case}"

        return result

    return str(audio)


async def trends_command(update, context):
    picks = load_trend_picks(update.effective_user.id)
    if not picks:
        await update.message.reply_text(
            "📡 No real trend data right now.\n\n"
            "The daily trend job fetches it (GitHub Actions → Daily Trend Brain). "
            "Nothing is guessed or AI-generated here."
        )
        return

    lines = ["🔥 VIBEOS TREND BRAIN", "Real data from YouTube India (a proxy for Reels, not Instagram data)", ""]
    lines.append("🎵 TRENDING SONGS")
    for i, t in enumerate(picks["audio"], 1):
        ev = t.get("evidence", {})
        views = f" · {ev['views']:,} views" if ev.get("views") else ""
        lines.append(f"{i}. {t['title']}")
        lines.append(f"   YouTube music chart #{ev.get('rank', '?')}{views} · seen {_ago(t['observed_at'])}")
    if not picks["audio"]:
        lines.append("None right now.")
    lines.append("⚠️ Check the audio exists on Instagram and is allowed for your account.")

    lines += ["", "#️⃣ HASHTAGS IN TOP SHORTS (last 7 days)"]
    for i, t in enumerate(picks["hashtag"], 1):
        ev = t.get("evidence", {})
        lines.append(f"{i}. {t['title']} — in {ev.get('videos', '?')} of {ev.get('of_shorts', '?')} top Shorts"
                     f" · seen {_ago(t['observed_at'])}")
    if not picks["hashtag"]:
        lines.append("None right now.")

    lines += ["", "📸 Send a photo and use /trendcaption."]
    await update.message.reply_text("\n".join(lines)[:4000])


# =========================================================
# START + VIBE
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    users.update_one(
        {"telegram_id": str(user.id)},
        {
            "$set": {
                "username": user.username,
                "first_name": user.first_name,
                "updated_at": now_utc(),
            },
            "$setOnInsert": {
                "telegram_id": str(user.id),
                "vibe": DEFAULT_VIBE,
                "created_at": now_utc(),
            },
        },
        upsert=True,
    )

    await update.message.reply_text(
        "🔥 VibeOS is ready!\n\n"
        "📸 Send a photo for AI editing.\n"
        "📸 Send an album to select the best photo.\n"
        "🗂️ /photos — view your saved photo IDs\n"
        "🗑️ /deletephoto ID — delete one photo\n"
        "⚠️ /deleteallphotos confirm — delete all your photo data\n\n"
        "🎨 /vibe baddie | soft | moody | natural | golden | clean | film\n\n"
        "✍️ /caption baddie  (styles: baddie soft funny natural moody golden clean film)\n"
        "✍️ /caption baddie hinglish  (Hinglish caption)\n"
        "🏷️ /hashtags  (5 precise tags, Instagram's limit)\n\n"
        "🔥 /trends\n"
        "✨ /trendcaption\n\n"
        "🧩 /grid\n"
        "📊 /gridscore\n"
        "🗑️ /gridreset\n\n"
        "📅 /schedule · /queue · /pause · /resume\n"
        "🤖 /autopilot"
    )


async def vibe_command(update, context):
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            f"Current vibe: {get_user_vibe(user_id)}\n"
            "Choose: " + ", ".join(EDITOR_STYLES) + ".\n"
            "Example: /vibe golden"
        )
        return

    try:
        vibe = memory.save_vibe(user_id, context.args[0])["vibe"]
    except ValueError:
        await update.message.reply_text(
            "Choose: " + ", ".join(EDITOR_STYLES) + "."
        )
        return

    await update.message.reply_text(f"✅ Vibe set to {vibe}!")


# =========================================================
# BEST PHOTO SELECTOR
# =========================================================

async def score_photo(image_bytes, vibe):
    prompt = f"""
Evaluate this photo for social-media presentation.
Preferred visual style: {vibe}.

Score composition, lighting, expression, pose/presentation,
background, aesthetic and post potential from 0 to 100.

Do not identify the person, infer sensitive information,
judge body attractiveness, or compare bodies.

Return ONLY JSON:
{{
  "composition": 0,
  "lighting": 0,
  "expression": 0,
  "pose": 0,
  "background": 0,
  "aesthetic": 0,
  "overall_post_potential": 0,
  "final_score": 0,
  "reason": "short explanation"
}}
"""

    try:
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model=MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,"
                            + image_to_base64(image_bytes)
                        },
                    },
                ],
            }],
            temperature=0.2,
        )

        result = parse_ai_json(
            response.choices[0].message.content
        )

        result["final_score"] = clamp_score(
            result.get("final_score", 0)
        )

        return result

    except Exception:
        logger.exception("Photo scoring failed")
        return {
            "final_score": 0,
            "reason": "AI scoring failed",
        }


# =========================================================
# SMART CAPTION + HASHTAG GENERATION
# =========================================================

CAPTION_STYLES = copywriter.CAPTION_STYLES
CAPTION_LANGUAGE = getattr(settings, "CAPTION_LANGUAGE", "english")  # english | hinglish


def recent_captions_for(user_id, limit=8):
    """Last captions of this user, so the AI does not repeat itself."""
    if user_id is None:
        return []
    try:
        rows = photos.find(
            {"telegram_id": str(user_id), "caption": {"$exists": True}},
            {"caption": 1},
        ).sort("created_at", -1).limit(limit)
        return [r.get("caption", "") for r in rows]
    except Exception:
        logger.exception("Could not load recent captions")
        return []


async def generate_social_copy(
    image_bytes,
    vibe,
    mode,
    trend_context=None,
    user_id=None,
    language=None,
):
    prompt = copywriter.build_prompt(
        mode=mode,
        vibe=vibe,
        language=language or CAPTION_LANGUAGE,
        trend_context=trend_context,
        recent_captions=recent_captions_for(user_id),
    )
    image_url = "data:image/jpeg;base64," + image_to_base64(image_bytes)
    last_error = None

    # One retry: vision models sometimes return broken JSON.
    for attempt in range(2):
        try:
            response = await asyncio.to_thread(
                groq_client.chat.completions.create,
                model=MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }],
                temperature=0.9 if attempt == 0 else 0.6,
            )
            data = parse_ai_json(response.choices[0].message.content)
            parsed = copywriter.parse_copy(data, mode=mode)
            parsed["overlay"] = clean_overlay(data.get("overlay", ""))
            return parsed
        except Exception as exc:
            last_error = exc
            logger.warning("Caption attempt %s failed: %s", attempt + 1, exc)

    raise ValueError(f"AI caption failed: {last_error}")


async def update_social_copy(
    photo_doc,
    image_bytes,
    style,
    update,
    context,
    trend_context=None,
    update_overlay=False,
    refresh_preview=True,
    language=None,
):
    result = await generate_social_copy(
        image_bytes,
        photo_doc.get("vibe", DEFAULT_VIBE),
        style,
        trend_context=trend_context,
        user_id=photo_doc.get("telegram_id"),
        language=language,
    )

    fields = {
        "caption": result["caption"],
        "hashtags": result["hashtags"],
        "caption_style": result["caption_style"],
        "copy_updated_at": now_utc(),
    }

    if update_overlay and result.get("overlay"):
        fields["overlay"] = result["overlay"]

    photos.update_one({"_id": photo_doc["_id"]}, {"$set": fields})

    refreshed = photos.find_one({"_id": photo_doc["_id"]})

    if (
        refresh_preview
        and refreshed
        and refreshed.get("preview_message_id")
        and refreshed.get("status") == "preview_ready"
    ):
        try:
            await context.bot.edit_message_caption(
                chat_id=update.effective_chat.id,
                message_id=refreshed["preview_message_id"],
                caption=format_preview_caption(refreshed),
                reply_markup=preview_keyboard(str(refreshed["_id"])),
            )
        except Exception:
            logger.info("Could not refresh old preview message")

    return result


# =========================================================
# CAPTION COMMAND
# =========================================================

async def caption_command(update, context):
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Usage: /caption <style> [hinglish]\n\n"
            "Styles: " + ", ".join(CAPTION_STYLES) + "\n"
            "Example: /caption baddie hinglish"
        )
        return

    style = context.args[0].lower()
    language = "hinglish" if any(
        a.lower() in {"hinglish", "hindi"} for a in context.args[1:]
    ) else ("english" if any(
        a.lower() == "english" for a in context.args[1:]) else None)

    if style not in CAPTION_STYLES:
        await update.message.reply_text(
            "Choose a style: " + ", ".join(CAPTION_STYLES) + "."
        )
        return

    photo_doc = get_latest_owned_photo(user_id)

    if not photo_doc:
        await update.message.reply_text(
            "📸 Send a photo first, then use /caption."
        )
        return

    status = await update.message.reply_text(
        f"✍️ Generating a {style} caption..."
    )

    try:
        file_id = photo_doc.get("original_file_id")

        if not file_id:
            raise ValueError("Original photo file is missing")

        image_bytes = await download_telegram_photo(
            context,
            file_id,
        )

        result = await update_social_copy(
            photo_doc,
            image_bytes,
            style,
            update,
            context,
            language=language,
        )

        await status.edit_text(
            f"✅ New {style} caption saved:\n\n"
            f"{result['caption']}\n\n"
            f"{' '.join(result['hashtags'])}"
        )

    except Exception:
        logger.exception("Caption command failed")
        await status.edit_text(
            "❌ Caption generation failed. Please try again."
        )


# =========================================================
# HASHTAGS COMMAND
# =========================================================

async def hashtags_command(update, context):
    photo_doc = get_latest_owned_photo(
        update.effective_user.id
    )

    if not photo_doc:
        await update.message.reply_text(
            "📸 Send a photo first, then use /hashtags."
        )
        return

    status = await update.message.reply_text(
        "🏷️ Generating relevant hashtags..."
    )

    try:
        file_id = photo_doc.get("original_file_id")

        if not file_id:
            raise ValueError("Original photo file is missing")

        image_bytes = await download_telegram_photo(
            context,
            file_id,
        )

        style = photo_doc.get(
            "caption_style",
            get_user_vibe(update.effective_user.id),
        )

        if style not in CAPTION_STYLES:
            style = "natural"

        result = await update_social_copy(
            photo_doc,
            image_bytes,
            style,
            update,
            context,
        )

        await status.edit_text(
            "🏷️ Hashtags saved:\n\n"
            + " ".join(result["hashtags"])
            + "\n\nThe preview has been refreshed if editable."
        )

    except Exception:
        logger.exception("Hashtag command failed")
        await status.edit_text(
            "❌ Hashtag generation failed. Please try again."
        )


# =========================================================
# TREND CAPTION COMMAND
# =========================================================

async def trendcaption_command(update, context):
    user_id = update.effective_user.id
    picks = load_trend_picks(user_id, limit=3)

    if not picks:
        await update.message.reply_text(
            "📡 No real trend data available right now, so no trend caption.\n"
            "Use the normal caption flow, or run the Daily Trend Brain workflow."
        )
        return

    photo_doc = get_latest_owned_photo(user_id)

    if not photo_doc:
        await update.message.reply_text(
            "📸 Pehle ek photo send karo, "
            "phir /trendcaption use karo."
        )
        return

    songs = [t["title"] for t in picks["audio"][:3]]
    tags = [t["title"] for t in picks["hashtag"][:5]]
    trend_context = (
        "Real trend signals (from YouTube India; a proxy, not Instagram data):\n"
        f"- Popular songs: {', '.join(songs) or 'none'}\n"
        f"- Hashtags common in top recent Shorts: {', '.join(tags) or 'none'}\n"
        "Use them only as mood/topic inspiration if they fit the photo. "
        "Do not claim any audio is trending on Instagram or licensed. "
        "Only use a hashtag if it truly fits the photo."
    )

    status = await update.message.reply_text(
        "🔥 Matching your photo with real trend signals..."
    )

    try:
        file_id = photo_doc.get("original_file_id")

        if not file_id:
            raise ValueError("Original photo file is missing")

        image_bytes = await download_telegram_photo(
            context,
            file_id,
        )

        style = photo_doc.get(
            "caption_style",
            photo_doc.get("vibe", DEFAULT_VIBE),
        )

        if style not in CAPTION_STYLES:
            style = "natural"

        result = await update_social_copy(
            photo_doc,
            image_bytes,
            style,
            update,
            context,
            trend_context=trend_context,
        )

        await status.edit_text(
            "🔥 TREND-AWARE COPY READY\n\n"
            f"💬 {result['caption']}\n\n"
            f"{' '.join(result['hashtags'])}\n\n"
            f"🎵 Popular songs (YouTube India): {', '.join(songs) or 'none'}\n"
            "ℹ️ Real YouTube data, not Instagram. Check the audio is available on Instagram first."
        )

    except Exception:
        logger.exception("Trend caption generation failed")
        await status.edit_text(
            "❌ Couldn't generate trend-inspired copy. Try again."
        )


# =========================================================
# INITIAL PHOTO ANALYSIS
# =========================================================

async def analyze_photo(image_bytes, vibe):
    prompt = f"""
You are VibeOS Photo Engine.
Analyze this image for social-media posting.
Preferred vibe: {vibe}.

Return ONLY JSON:
{{
  "description": "short visual description",
  "filter": "{vibe}"
}}

Allowed filters: {", ".join(EDITOR_STYLES)}.
Focus on composition, lighting, colors and environment.
Do not identify the person or infer sensitive information.
"""

    try:
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model=MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,"
                            + image_to_base64(image_bytes)
                        },
                    },
                ],
            }],
            temperature=0.5,
        )

        data = parse_ai_json(
            response.choices[0].message.content
        )

        if data.get("filter") not in EDITOR_STYLES:
            data["filter"] = vibe

        return data

    except Exception:
        logger.exception("Photo analysis failed")
        return {
            "description": "Photo analysis unavailable",
            "filter": vibe,
        }


# =========================================================
# PHOTO PREVIEW
# =========================================================

async def send_preview(
    update,
    context,
    image_bytes,
    analysis,
    vibe,
    original_file_id,
    asset_id=None,
):
    style = analysis.get("filter", vibe)

    social_copy = await generate_social_copy(
        image_bytes,
        vibe,
        vibe if vibe in CAPTION_STYLES else "natural",
        user_id=update.effective_user.id,
    )

    overlay = social_copy.get("overlay", "")

    edited_image = await edit_via_worker(
        image_bytes,
        style,
        "post",
        overlay or None,
        "minimal",
    )

    photo_result = photos.insert_one({
        "telegram_id": str(update.effective_user.id),
        "asset_id": asset_id,
        "original_file_id": original_file_id,
        "edited_file_id": None,
        "vibe": vibe,
        "analysis": analysis,
        "caption": social_copy["caption"],
        "hashtags": social_copy["hashtags"],
        "caption_style": social_copy["caption_style"],
        "overlay": overlay,
        "render_size": "post",
        "text_style": "minimal",
        "show_text": True,
        "status": "preview_ready",
        "created_at": now_utc(),
    })

    photo_id = str(photo_result.inserted_id)

    from telegram.error import NetworkError
    preview_caption = format_preview_caption({
        "analysis": analysis,
        "vibe": vibe,
        "caption": social_copy["caption"],
        "hashtags": social_copy["hashtags"],
        "render_size": "post",
        "text_style": "minimal",
        "show_text": True,
        "status": "preview_ready",
    })
    for attempt in range(3):  # mobile networks drop connections (httpx.ReadError)
        try:
            if hasattr(edited_image, "seek"):
                edited_image.seek(0)
            message = await update.message.reply_photo(
                photo=edited_image,
                caption=preview_caption,
                reply_markup=preview_keyboard(photo_id),
            )
            break
        except NetworkError:
            if attempt == 2:
                raise
            logger.warning("send_photo network error, retrying (%s/3)", attempt + 1)
            await asyncio.sleep(2 * (attempt + 1))

    edited_file_id = (
        message.photo[-1].file_id if message.photo else None
    )

    photos.update_one(
        {"_id": photo_result.inserted_id},
        {
            "$set": {
                "edited_file_id": edited_file_id,
                "preview_message_id": message.message_id,
                "preview_chat_id": message.chat_id,
            }
        },
    )


async def run_photo_engine(
    update, context, image_bytes, file_id, vibe, asset_id=None
):
    status = None

    try:
        status = await update.message.reply_text(
            "🧠 Analyzing, editing and writing your caption..."
        )

        analysis = await analyze_photo(image_bytes, vibe)

        await send_preview(
            update,
            context,
            image_bytes,
            analysis,
            vibe,
            file_id,
            asset_id,
        )

        await status.delete()

    except Exception:
        logger.exception("Photo engine failed")

        if status:
            try:
                await status.edit_text(
                    "❌ Processing failed. Please try again."
                )
            except Exception:
                pass
        else:
            await update.message.reply_text(
                "❌ Processing failed. Please try again."
            )


# =========================================================
# ALBUM PROCESSING
# =========================================================

REJECT_LABELS = {
    "blurry": "blurry",
    "too_dark": "too dark",
    "too_bright": "too bright",
    "too_small": "low resolution",
    "unreadable": "unreadable file",
    "duplicate_in_batch": "duplicate shot",
    "duplicate_of_used": "already used before",
}


def buffer_filter(user_id, batch_key, downloaded, n=None):
    """Run intake -> analysis -> shortlist. Returns (shortlist, summary)."""
    report = content_buffer.process_batch(
        user_id,
        batch_key,
        [(d["image_bytes"], d["file_id"]) for d in downloaded],
        n=n or SHORTLIST_SIZE,
    )

    selected_files = set()
    for asset_id in report["selected"]:
        asset = content_buffer.assets.find_one({"_id": asset_id})
        if asset:
            selected_files.add(asset["filename"])

    index_by_file = {d["file_id"]: d["index"] for d in downloaded}
    problems = []
    for asset in content_buffer.list_assets(
        user_id, status="rejected", batch_id=batch_key
    ):
        reason = (asset.get("reject_reason") or "").replace("quality:", "")
        labels = [
            REJECT_LABELS.get(r, r) for r in reason.split(",") if r
        ]
        idx = index_by_file.get(asset["filename"])
        if idx:
            problems.append(f"#{idx} {', '.join(labels)}")

    shortlist = [d for d in downloaded if d["file_id"] in selected_files]

    lines = [f"🧹 Quality check: {len(shortlist)} shortlisted"]
    if problems:
        lines.append("Skipped: " + "; ".join(problems))
    if report.get("exact_duplicates"):
        lines.append(
            f"{report['exact_duplicates']} already in your buffer"
        )
    if report.get("quota_blocked"):
        lines.append(
            f"⚠️ Daily upload limit reached: {report['quota_blocked']} "
            "photo(s) were not added. Try again tomorrow."
        )
    return shortlist, "\n".join(lines)


def pick_best_asset(user_id, batch_key, best_file_id):
    """Link the chosen photo to its buffer asset; release the other
    shortlisted ones so they don't block future similar photos."""
    asset_id = None
    for asset in content_buffer.list_assets(
        user_id, status="selected", batch_id=batch_key
    ):
        if asset["filename"] == best_file_id:
            asset_id = asset["_id"]
        else:
            content_buffer.release(asset["_id"])
    return asset_id


def buffer_single(user_id, file_id, image_bytes):
    """Single photo through the same buffer checks. -> (asset_id, error)."""
    batch_key = f"single:{uuid.uuid4().hex}"
    report = content_buffer.process_batch(
        user_id, batch_key, [(image_bytes, file_id)], n=1
    )
    if report["selected"]:
        return report["selected"][0], None
    if report.get("exact_duplicates"):
        return None, "This exact photo is already in your buffer."
    for asset in content_buffer.list_assets(
        user_id, status="rejected", batch_id=batch_key
    ):
        reason = (asset.get("reject_reason") or "").replace("quality:", "")
        labels = [REJECT_LABELS.get(r, r) for r in reason.split(",") if r]
        return None, "Skipped: " + ", ".join(labels)
    return None, "Photo couldn't be shortlisted."


async def auto_prepare(user_id, image_bytes, file_id, asset_id, vibe):
    """FULL AUTO: analyze -> caption -> edit -> mark publish-ready. No approval."""
    analysis = await analyze_photo(image_bytes, vibe)
    social_copy = await generate_social_copy(
        image_bytes, vibe, vibe if vibe in CAPTION_STYLES else "natural",
        user_id=user_id,
    )
    style = analysis.get("filter", vibe)
    overlay = social_copy.get("overlay") or None

    await asyncio.to_thread(
        content_buffer.prepare_ready_job,
        asset_id,
        social_copy["caption"],
        social_copy["hashtags"],
        style,
        ("post", "story"),
        style=style,
        overlay=overlay,
        text_style="minimal",
    )
    photos.insert_one({
        "telegram_id": str(user_id),
        "asset_id": asset_id,
        "original_file_id": file_id,
        "vibe": vibe,
        "analysis": analysis,
        "caption": social_copy["caption"],
        "hashtags": social_copy["hashtags"],
        "caption_style": social_copy.get("caption_style"),
        "overlay": overlay or "",
        "status": "auto_approved",
        "created_at": now_utc(),
    })
    return social_copy["caption"]


async def run_full_auto(update, user_id, vibe, items):
    """items: [{file_id, image_bytes, asset_id, score}] -> one summary message."""
    done, low, failed = 0, 0, 0
    for item in items:
        if item.get("asset_id") is None:
            failed += 1
            continue
        if item.get("score", 100) < MIN_AUTO_SCORE:
            low += 1
            try:
                await asyncio.to_thread(content_buffer.release, item["asset_id"])
            except Exception:
                logger.exception("release failed")
            continue
        try:
            await auto_prepare(
                user_id, item["image_bytes"], item["file_id"],
                item["asset_id"], vibe,
            )
            done += 1
        except Exception:
            logger.exception("Full-auto prepare failed")
            failed += 1

    lines = [f"🚀 Full auto: {done} photo(s) edited and queued for posting."]
    if low:
        lines.append(f"🧹 {low} skipped (AI score below {MIN_AUTO_SCORE}).")
    if failed:
        lines.append(f"⚠️ {failed} failed — send them again.")
    if done:
        lines.append(
            "They post into your autopilot slots. /queue to watch, "
            "/pause to stop. (Instagram must be connected: /igstatus)"
        )
    await update.message.reply_text("\n".join(lines))


async def process_single(update, context, image_bytes, file_id, vibe):
    user_id = update.effective_user.id
    asset_id = None
    try:
        asset_id, error = await asyncio.to_thread(
            buffer_single, user_id, file_id, image_bytes
        )
        if error:
            await update.message.reply_text(f"🧹 {error}")
            return
    except Exception:
        logger.exception("Buffer check failed; continuing without buffer")
    if asset_id is not None and full_auto_on(user_id):
        try:
            score = await score_photo(image_bytes, vibe)
        except Exception:
            logger.exception("Scoring failed in full auto")
            score = {"final_score": 0}
        await run_full_auto(update, user_id, vibe, [{
            "file_id": file_id, "image_bytes": image_bytes,
            "asset_id": asset_id, "score": score.get("final_score", 0),
        }])
        return
    await run_photo_engine(
        update, context, image_bytes, file_id, vibe, asset_id
    )


async def process_photo_batch(
    update,
    context,
    media_group_id,
    file_ids,
):
    user_id = update.effective_user.id
    vibe = get_user_vibe(user_id)

    if not file_ids:
        return

    if len(file_ids) < 2:
        image_bytes = await download_telegram_photo(
            context,
            file_ids[0],
        )

        if len(image_bytes) > MAX_PHOTO_SIZE:
            await update.message.reply_text(
                "❌ Photo exceeds 10 MB."
            )
            return

        await process_single(
            update, context, image_bytes, file_ids[0], vibe
        )
        return

    await update.message.reply_text(
        f"🧠 Checking {len(file_ids)} photos..."
    )

    downloaded = []

    for index, file_id in enumerate(file_ids, start=1):
        try:
            image_bytes = await download_telegram_photo(
                context,
                file_id,
            )

            if len(image_bytes) > MAX_PHOTO_SIZE:
                continue

            downloaded.append({
                "index": index,
                "file_id": file_id,
                "image_bytes": image_bytes,
            })

        except Exception:
            logger.exception("Album photo download failed")

    if not downloaded:
        await update.message.reply_text(
            "❌ Couldn't process any photos in the album."
        )
        return

    # Phase 1: quality + duplicate filter before spending AI calls.
    shortlist = downloaded
    try:
        shortlist, summary = await asyncio.to_thread(
            buffer_filter, user_id, str(media_group_id), downloaded,
            len(downloaded) if full_auto_on(user_id) else None,
        )
        if summary:
            await update.message.reply_text(summary)
    except Exception:
        logger.exception("Buffer filter failed; using all photos")
        shortlist = downloaded

    if not shortlist:
        await update.message.reply_text(
            "❌ No usable photos in this album. "
            "Try sharper, brighter, different shots."
        )
        return

    scored = []

    for item in shortlist:
        try:
            score = await score_photo(item["image_bytes"], vibe)

            scored.append({
                "index": item["index"],
                "file_id": item["file_id"],
                "image_bytes": item["image_bytes"],
                "score": score,
            })

        except Exception:
            logger.exception("Album photo scoring failed")

    if not scored:
        await update.message.reply_text(
            "❌ Couldn't process any photos in the album."
        )
        return

    scored.sort(
        key=lambda item: item["score"].get("final_score", 0),
        reverse=True,
    )

    best = scored[0]

    batches.insert_one({
        "telegram_id": str(user_id),
        "media_group_id": media_group_id,
        "vibe": vibe,
        "total_photos": len(file_ids),
        "processed_photos": len(scored),
        "selected_index": best["index"],
        "selected_file_id": best["file_id"],
        "selected_score": best["score"].get("final_score", 0),
        "reason": best["score"].get("reason", ""),
        "scores": [
            {
                "index": item["index"],
                "file_id": item["file_id"],
                "score": item["score"],
            }
            for item in scored
        ],
        "created_at": now_utc(),
    })

    if full_auto_on(user_id):
        items = []
        for it in scored:
            asset = await asyncio.to_thread(
                content_buffer.asset_for_file,
                user_id, str(media_group_id), it["file_id"], "selected",
            )
            items.append({
                "file_id": it["file_id"], "image_bytes": it["image_bytes"],
                "asset_id": asset["_id"] if asset else None,
                "score": it["score"].get("final_score", 0),
            })
        await run_full_auto(update, user_id, vibe, items)
        return

    await update.message.reply_text(
        f"🏆 Best photo: #{best['index']}\n"
        f"🔥 Score: {best['score'].get('final_score', 0)}/100\n"
        f"💡 {best['score'].get('reason', 'Best overall result')}"
    )

    asset_id = None
    try:
        asset_id = await asyncio.to_thread(
            pick_best_asset, user_id, str(media_group_id), best["file_id"]
        )
    except Exception:
        logger.exception("Could not link best photo to buffer asset")

    await run_photo_engine(
        update,
        context,
        best["image_bytes"],
        best["file_id"],
        vibe,
        asset_id,
    )


async def finalize_batch(key, update, context):
    try:
        await asyncio.sleep(2.0)

        batch = photo_batches.get(key)

        if not batch:
            return

        photo_batches.pop(key, None)

        await process_photo_batch(
            update,
            context,
            key,
            batch["file_ids"][:MAX_ALBUM_PHOTOS],
        )

    except asyncio.CancelledError:
        return

    except Exception:
        logger.exception("Album processing failed")

        try:
            await update.message.reply_text(
                "❌ Album processing failed. Please try again."
            )
        except Exception:
            pass


async def photo_handler(update, context):
    message = update.message

    if not message or not message.photo:
        return

    file_id = message.photo[-1].file_id

    if message.media_group_id:
        key = f"{message.chat_id}:{message.media_group_id}"
        batch = photo_batches.get(key)

        if batch is None:
            batch = {"file_ids": [], "task": None}
            photo_batches[key] = batch

        if (
            file_id not in batch["file_ids"]
            and len(batch["file_ids"]) < MAX_ALBUM_PHOTOS
        ):
            batch["file_ids"].append(file_id)

        old_task = batch.get("task")

        if old_task and not old_task.done():
            old_task.cancel()

        batch["task"] = asyncio.create_task(
            finalize_batch(key, update, context)
        )
        return

    try:
        image_bytes = await download_telegram_photo(
            context,
            file_id,
        )

        if len(image_bytes) > MAX_PHOTO_SIZE:
            await message.reply_text("❌ Photo exceeds 10 MB.")
            return

        await process_single(
            update,
            context,
            image_bytes,
            file_id,
            get_user_vibe(update.effective_user.id),
        )

    except Exception:
        logger.exception("Photo handler failed")
        await message.reply_text(
            "❌ Couldn't process this photo. Please try again."
        )


# =========================================================
# APPROVE / DISCARD / REGENERATE BUTTONS
# =========================================================

async def rerender_preview(query, context, doc, **changes):
    """Re-edit the original photo with new render settings and swap the preview."""
    object_id = doc["_id"]

    if changes:
        photos.update_one({"_id": object_id}, {"$set": changes})

    doc = photos.find_one({"_id": object_id})
    settings = render_settings(doc)

    image_bytes = await download_telegram_photo(
        context,
        doc["original_file_id"],
    )

    style = doc.get("analysis", {}).get("filter", doc.get("vibe", DEFAULT_VIBE))
    overlay = doc.get("overlay") if settings["show_text"] else None

    edited = await edit_via_worker(
        image_bytes,
        style,
        settings["size"],
        overlay or None,
        settings["text_style"],
        "bottom",
    )

    message = await query.edit_message_media(
        media=InputMediaPhoto(
            media=edited,
            caption=format_preview_caption(doc),
        ),
        reply_markup=preview_keyboard(str(object_id)),
    )

    if getattr(message, "photo", None):
        photos.update_one(
            {"_id": object_id},
            {"$set": {"edited_file_id": message.photo[-1].file_id}},
        )


async def _alert(query, text):
    """Show an alert; Telegram allows only one answer() per callback."""
    try:
        await query.answer(text, show_alert=True)
    except Exception:
        try:
            await query.message.reply_text(text)
        except Exception:
            logger.warning("Could not deliver alert: %s", text)


async def button_handler(update, context):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    user_id = str(query.from_user.id)
    parts = query.data.split(":", 1)

    if len(parts) != 2:
        return

    action, photo_id = parts

    try:
        object_id = ObjectId(photo_id)
    except Exception:
        await _alert(query, "Invalid photo reference.")
        return

    doc = photos.find_one({
        "_id": object_id,
        "telegram_id": user_id,
    })

    if not doc:
        await _alert(query, "Photo not found or not yours.")
        return

    if action in {"story", "tstyle", "ttoggle"}:
        if doc.get("status") != "preview_ready":
            await _alert(
                query,
                "Editing is available on a pending preview.",
            )
            return

        try:
            settings = render_settings(doc)

            if action == "story":
                new_size = "post" if settings["size"] == "story" else "story"
                changes = {"render_size": new_size}
            elif action == "tstyle":
                index = TEXT_STYLES.index(settings["text_style"])
                changes = {
                    "text_style": TEXT_STYLES[(index + 1) % len(TEXT_STYLES)]
                }
            else:
                changes = {"show_text": not settings["show_text"]}

            await rerender_preview(query, context, doc, **changes)

        except Exception:
            logger.exception("Preview re-render failed")
            await _alert(
                query,
                "Couldn't update the photo. Try again.",
            )

        return

    if action in {"regencap", "hashtags"}:
        if doc.get("status") != "preview_ready":
            await _alert(
                query,
                "Caption editing is available on a pending preview.",
            )
            return

        try:
            await query.edit_message_caption(
                caption="✍️ Generating updated social copy..."
            )

            image_bytes = await download_telegram_photo(
                context,
                doc["original_file_id"],
            )

            style = doc.get(
                "caption_style",
                doc.get("vibe", DEFAULT_VIBE),
            )

            if style not in CAPTION_STYLES:
                style = "natural"

            await update_social_copy(
                doc,
                image_bytes,
                style,
                update,
                context,
                update_overlay=(action == "regencap"),
                refresh_preview=False,
            )

            refreshed = photos.find_one({"_id": object_id})

            if action == "regencap":
                await rerender_preview(query, context, refreshed)
            else:
                await query.edit_message_caption(
                    caption=format_preview_caption(refreshed),
                    reply_markup=preview_keyboard(photo_id),
                )

        except Exception:
            logger.exception("Preview copy action failed")

            try:
                await query.edit_message_caption(
                    caption="❌ Couldn't update the caption. Try /caption baddie."
                )
            except Exception:
                pass

        return

    if action == "approve":
        result = photos.update_one(
            {
                "_id": object_id,
                "telegram_id": user_id,
                "status": "preview_ready",
            },
            {
                "$set": {
                    "status": "approved",
                    "approved_at": now_utc(),
                }
            },
        )

        if result.modified_count == 0:
            await _alert(
                query,
                "Already approved or discarded.",
            )
            return

        # Phase 1: approval turns the selected asset into publish-ready content.
        asset_id = doc.get("asset_id")
        ready_note = ""
        if asset_id:
            style = (doc.get("analysis") or {}).get(
                "filter", doc.get("vibe", DEFAULT_VIBE)
            )
            overlay = doc.get("overlay") if doc.get("show_text", True) else None
            text_style = doc.get("text_style", "minimal")

            try:
                await asyncio.to_thread(
                    content_buffer.prepare_ready_job,
                    asset_id,
                    doc.get("caption", ""),
                    doc.get("hashtags", []),
                    style,
                    ("post", "story"),
                    style=style,
                    overlay=overlay or None,
                    text_style=text_style,
                )
                ready_note = (
                    "📦 Publish-ready: post + story versions saved.\n\n"
                )
            except Exception:
                logger.exception("prepare_ready failed for %s", asset_id)
                # Roll back so the user can tap Approve again.
                photos.update_one(
                    {"_id": object_id, "telegram_id": user_id},
                    {"$set": {"status": "preview_ready"},
                     "$unset": {"approved_at": ""}},
                )
                await _alert(
                    query, "Couldn't prepare publish-ready files. Try again."
                )
                return

        edited_file_id = doc.get("edited_file_id")

        if edited_file_id:
            try:
                grid_entries.update_one(
                    {
                        "telegram_id": user_id,
                        "photo_id": photo_id,
                    },
                    {
                        "$setOnInsert": {
                            "telegram_id": user_id,
                            "photo_id": photo_id,
                            "edited_file_id": edited_file_id,
                            "vibe": doc.get("vibe", DEFAULT_VIBE),
                            "created_at": now_utc(),
                        }
                    },
                    upsert=True,
                )
            except DuplicateKeyError:
                pass

        await query.edit_message_caption(
            caption=(
                "✅ Approved!\n\n"
                + ready_note +
                "Your edited photo has been added to Vibe Lock "
                "when its Telegram file ID is available.\n\n"
                "Use /grid to view your grid."
            )
        )
        return

    if action == "discard":
        result = photos.update_one(
            {
                "_id": object_id,
                "telegram_id": user_id,
                "status": "preview_ready",
            },
            {
                "$set": {
                    "status": "discarded",
                    "discarded_at": now_utc(),
                }
            },
        )

        if result.modified_count == 0:
            await _alert(
                query,
                "Already approved or discarded.",
            )
            return

        if doc.get("asset_id"):
            await asyncio.to_thread(
                content_buffer.discard, doc["asset_id"]
            )

        await query.edit_message_caption(
            caption="🗑️ Discarded. This photo won't be added to your grid."
        )


# =========================================================
# VIBE LOCK: GRID IMAGE
# =========================================================

def get_grid_entries(user_id):
    return list(
        grid_entries.find(
            {"telegram_id": str(user_id)}
        ).sort(
            [("created_at", pymongo.DESCENDING)]
        ).limit(9)
    )


async def build_grid_image(entries, context):
    cell_size = 300

    canvas = Image.new(
        "RGB",
        (cell_size * 3, cell_size * 3),
        (35, 35, 35),
    )

    draw = ImageDraw.Draw(canvas)

    for index, entry in enumerate(entries[:9]):
        x = (index % 3) * cell_size
        y = (index // 3) * cell_size

        try:
            image_bytes = await download_telegram_photo(
                context,
                entry["edited_file_id"],
            )

            image = Image.open(
                io.BytesIO(image_bytes)
            ).convert("RGB")

            image = ImageOps.fit(
                image,
                (cell_size, cell_size),
                method=Image.Resampling.LANCZOS,
            )

            canvas.paste(image, (x, y))

        except Exception:
            logger.exception("Grid image unavailable")

            draw.rectangle(
                (x, y, x + cell_size, y + cell_size),
                fill=(65, 65, 65),
            )

            draw.text(
                (x + 70, y + 140),
                "Image unavailable",
                fill="white",
            )

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=90, optimize=True)
    output.seek(0)

    return output


async def grid_command(update, context):
    entries = get_grid_entries(update.effective_user.id)

    if not entries:
        await update.message.reply_text(
            "🧩 Your Vibe Lock grid is empty.\n"
            "Send a photo and tap APPROVE to add it."
        )
        return

    try:
        grid_image = await build_grid_image(entries, context)

        await update.message.reply_photo(
            photo=grid_image,
            caption=(
                f"🧩 Vibe Lock — {len(entries)}/9 approved photos\n\n"
                "Newest approved photos appear first.\n"
                "Use /gridscore to analyze consistency."
            ),
        )

    except Exception:
        logger.exception("Grid preview failed")
        await update.message.reply_text(
            "❌ Couldn't build your grid."
        )


# =========================================================
# GRID CONSISTENCY SCORE
# =========================================================

async def score_grid_image(grid_bytes):
    prompt = """
Evaluate this 3x3 social-media grid on:
- color harmony
- visual-style consistency
- lighting consistency
- composition balance
- overall cohesion

Do not identify people, infer sensitive information,
or judge body attractiveness.

Return ONLY valid JSON:
{
  "consistency_score": 0,
  "color_harmony": 0,
  "style_consistency": 0,
  "composition_balance": 0,
  "suggestion": "one practical suggestion"
}
All scores must be between 0 and 100.
"""

    response = await asyncio.to_thread(
        groq_client.chat.completions.create,
        model=MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64,"
                        + image_to_base64(grid_bytes)
                    },
                },
            ],
        }],
        temperature=0.2,
    )

    result = parse_ai_json(
        response.choices[0].message.content
    )

    for key in (
        "consistency_score",
        "color_harmony",
        "style_consistency",
        "composition_balance",
    ):
        result[key] = clamp_score(result.get(key, 0))

    return result


async def gridscore_command(update, context):
    entries = get_grid_entries(update.effective_user.id)

    if not entries:
        await update.message.reply_text(
            "📊 Your grid is empty. Approve some photos first."
        )
        return

    status = await update.message.reply_text(
        "🧠 Analyzing grid consistency..."
    )

    try:
        grid_image = await build_grid_image(entries, context)
        result = await score_grid_image(grid_image.getvalue())

        await status.edit_text(
            "📊 VIBE LOCK — GRID SCORE\n\n"
            f"✨ Consistency: {result['consistency_score']}/100\n"
            f"🎨 Color harmony: {result['color_harmony']}/100\n"
            f"🪄 Style consistency: {result['style_consistency']}/100\n"
            f"🧩 Composition balance: {result['composition_balance']}/100\n\n"
            f"💡 {result.get('suggestion', 'Keep your style consistent.')}\n\n"
            f"Based on {len(entries)} approved photo(s)."
        )

    except Exception:
        logger.exception("Grid scoring failed")
        await status.edit_text(
            "❌ Couldn't calculate grid score. Try again."
        )


# =========================================================
# GRID RESET
# =========================================================

async def gridreset_command(update, context):
    user_id = str(update.effective_user.id)

    count = grid_entries.count_documents({
        "telegram_id": user_id
    })

    if count == 0:
        await update.message.reply_text(
            "Your grid is already empty."
        )
        return

    grid_entries.delete_many({"telegram_id": user_id})

    await update.message.reply_text(
        f"🗑️ Grid reset complete. Removed {count} grid entr(y/ies).\n"
        "Your photo records are still saved."
    )


# =========================================================
# PHOTO LIBRARY + PERMANENT DELETE
# =========================================================

async def photos_command(update, context):
    """List this user's saved photo IDs, statuses and buffer assets."""
    uid = str(update.effective_user.id)
    legacy = list(photos.find({"telegram_id": uid}).sort("created_at", pymongo.DESCENDING).limit(15))
    assets = content_buffer.list_assets(uid)
    assets.sort(key=lambda a: a.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True)
    lines = ["🗂 YOUR VIBEOS PHOTOS", "Use /deletephoto <ID> to permanently delete one."]
    seen = set()
    for doc in legacy:
        asset_id = str(doc.get("asset_id") or "")
        photo_id = asset_id or str(doc.get("_id", ""))
        if not photo_id or photo_id in seen:
            continue
        seen.add(photo_id)
        lines.append(f"• {photo_id} — {doc.get('status', 'saved')} (photo)")
    for asset in assets:
        asset_id = str(asset.get("_id", ""))
        if not asset_id or asset_id in seen:
            continue
        seen.add(asset_id)
        analysis = asset.get("analysis") or {}
        score = analysis.get("quality_score")
        score_text = f", score {score}" if score is not None else ""
        lines.append(f"• {asset_id} — {asset.get('status', 'saved')}{score_text} (buffer)")
    if not seen:
        await update.message.reply_text("📭 You have no saved photos.")
        return
    lines.append("\nDelete everything: /deleteallphotos confirm")
    await update.message.reply_text("\n".join(lines[:35]))


def _delete_jobs_for_assets(uid, asset_ids):
    """Cancel queued work before deleting its source media; never delete mid-publish."""
    asset_ids = [str(a) for a in asset_ids if a]
    if not asset_ids:
        return True, 0
    jobs = db["publish_jobs"]
    owned = {"telegram_id": str(uid), "asset_ids": {"$in": asset_ids}}
    if jobs.find_one({**owned, "status": "running"}):
        return False, 0
    result = jobs.update_many(
        {**owned, "status": {"$in": ["queued", "retry", "held", "needs_reauth", "needs_attention"]}},
        {"$set": {
            "status": "cancelled",
            "updated_at": now_utc(),
            "last_error": "Cancelled because the source photo was deleted by its owner.",
        }},
    )
    return True, int(getattr(result, "modified_count", 0))


async def deletephoto_command(update, context):
    """Permanently remove one owned legacy photo and/or content-buffer asset."""
    uid = str(update.effective_user.id)
    if len(context.args) != 1:
        await update.message.reply_text(
            "Usage: /deletephoto <ID>\nUse /photos to see your photo IDs."
        )
        return
    photo_id = context.args[0].strip()
    asset = content_buffer.assets.find_one({"_id": photo_id, "telegram_id": uid})
    query = {"telegram_id": uid}
    if ObjectId.is_valid(photo_id):
        query["_id"] = ObjectId(photo_id)
        legacy = photos.find_one(query)
    else:
        query["_id"] = photo_id
        legacy = photos.find_one(query)
    related_legacy = list(photos.find({"telegram_id": uid, "asset_id": photo_id}))
    if not asset and not legacy and not related_legacy:
        await update.message.reply_text(
            "❌ Photo ID not found in your account. Use /photos to see your IDs."
        )
        return

    asset_ids = []
    if asset:
        asset_ids.append(str(asset["_id"]))
    for doc in ([legacy] if legacy else []) + related_legacy:
        if doc.get("asset_id"):
            asset_ids.append(str(doc["asset_id"]))
    safe, cancelled = _delete_jobs_for_assets(uid, asset_ids)
    if not safe:
        await update.message.reply_text(
            "⏳ This photo is part of a post currently publishing. Wait until it finishes, then delete it."
        )
        return

    deleted_assets = 0
    for asset_id in set(asset_ids):
        if content_buffer.delete_asset(uid, asset_id):
            deleted_assets += 1
    legacy_ids = {str(doc["_id"]) for doc in ([legacy] if legacy else []) + related_legacy}
    if legacy:
        photos.delete_one({"_id": legacy["_id"], "telegram_id": uid})
    if related_legacy:
        photos.delete_many({"telegram_id": uid, "asset_id": photo_id})
    grid_ids = legacy_ids | {photo_id} | set(asset_ids)
    grid_entries.delete_many({"telegram_id": uid, "photo_id": {"$in": list(grid_ids)}})

    await update.message.reply_text(
        "🗑️ Photo permanently deleted from your VIBEOS database.\n"
        f"• Buffer assets removed: {deleted_assets}\n"
        f"• Pending publish jobs cancelled: {cancelled}\n"
        "This does not delete a post that was already published on Instagram."
    )


async def deleteallphotos_command(update, context):
    """Delete all of the caller's photo records, assets, and grid entries."""
    uid = str(update.effective_user.id)
    if not context.args or context.args[0].lower() != "confirm":
        await update.message.reply_text(
            "⚠️ This permanently deletes ALL your saved VIBEOS photo records, "
            "buffer images and grid entries.\n\n"
            "To confirm, send exactly:\n/deleteallphotos confirm"
        )
        return

    asset_docs = content_buffer.list_assets(uid)
    asset_ids = [str(a["_id"]) for a in asset_docs]
    safe, cancelled = _delete_jobs_for_assets(uid, asset_ids)
    if not safe:
        await update.message.reply_text(
            "⏳ A photo is part of a post currently publishing. Nothing was deleted. "
            "Wait for it to finish, then run /deleteallphotos confirm again."
        )
        return

    deleted_assets = await asyncio.to_thread(content_buffer.delete_all_assets, uid)
    legacy_count = photos.count_documents({"telegram_id": uid})
    photos.delete_many({"telegram_id": uid})
    grid_count = grid_entries.count_documents({"telegram_id": uid})
    grid_entries.delete_many({"telegram_id": uid})
    # Batch documents are metadata only; delete only this user's batches.
    batches.delete_many({"telegram_id": uid})

    await update.message.reply_text(
        "🗑️ ALL YOUR VIBEOS PHOTO DATA HAS BEEN DELETED.\n"
        f"• Buffer assets and stored image variants: {deleted_assets}\n"
        f"• Legacy photo records: {legacy_count}\n"
        f"• Grid entries: {grid_count}\n"
        f"• Pending publish jobs cancelled: {cancelled}\n\n"
        "Other users' data is untouched. Already-published Instagram posts are not removed."
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def buffer_command(update, context):
    stats = await asyncio.to_thread(
        content_buffer.stats, update.effective_user.id
    )
    await update.message.reply_text(
        "📦 Your content buffer\n"
        f"• uploaded: {stats['uploaded']}\n"
        f"• analyzed: {stats['analyzed']}\n"
        f"• selected: {stats['selected']}\n"
        f"• ready: {stats['ready']}\n"
        f"• rejected: {stats['rejected']}"
    )


async def ready_command(update, context):
    assets = await asyncio.to_thread(
        content_buffer.list_assets, update.effective_user.id, "ready"
    )
    if not assets:
        await update.message.reply_text(
            "📭 No publish-ready content yet. Send photos and tap Approve."
        )
        return
    lines = [f"✅ {len(assets)} publish-ready:"]
    for a in assets[-10:]:
        cap = (a["creative"]["caption"] or "").strip().replace("\n", " ")
        lines.append(f"• {cap[:60] or '(no caption)'}")
    await update.message.reply_text("\n".join(lines))


async def error_handler(update, context):
    from telegram.error import NetworkError
    if isinstance(context.error, NetworkError):
        # Flaky mobile connection: python-telegram-bot retries polling by itself.
        logger.warning("Telegram network hiccup: %s", context.error)
        return
    logger.error(
        "Unhandled Telegram error",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    logger.info("VibeOS Photo Engine starting...")
    memory.ensure_indexes()  # also verifies MongoDB is reachable
    content_buffer.ensure_indexes()
    jobs.ensure_indexes(db)   # media_jobs: (status, type, created) + finished-job TTL
    # Phase 2: Instagram publishing (None if Meta settings are not filled in settings.py)
    publishing = publish_bot.build_runtime(db, content_buffer, BOT_TOKEN)

    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=30,
    )

    async def _on_shutdown(_app):
        # Stop the Phase 3 worker + web server before the process exits.
        await asyncio.to_thread(publish_bot.shutdown, publishing)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .post_shutdown(_on_shutdown)
        .build()
    )

    # Abuse protection: runs before every other handler (group -1) and only
    # blocks a user who exceeds the per-user limits. No feature is changed.
    app.add_handler(TypeHandler(Update, make_gate()), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("vibe", vibe_command))
    app.add_handler(CommandHandler("caption", caption_command))
    app.add_handler(CommandHandler("hashtags", hashtags_command))

    app.add_handler(CommandHandler("trends", trends_command))
    app.add_handler(
        CommandHandler("trendcaption", trendcaption_command)
    )

    app.add_handler(CommandHandler("photos", photos_command))
    app.add_handler(CommandHandler("deletephoto", deletephoto_command))
    app.add_handler(CommandHandler("deleteallphotos", deleteallphotos_command))
    app.add_handler(CommandHandler("buffer", buffer_command))
    app.add_handler(CommandHandler("ready", ready_command))
    app.add_handler(CommandHandler("grid", grid_command))
    app.add_handler(CommandHandler("gridscore", gridscore_command))
    app.add_handler(CommandHandler("gridreset", gridreset_command))

    app.add_handler(
        MessageHandler(filters.PHOTO, photo_handler)
    )

    # Phase 2 handlers first: their "ig:" callbacks must not reach button_handler.
    publish_bot.register(app, publishing, content_buffer)

    app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    app.add_error_handler(error_handler)

    # Phase 3: start the always-on publishing worker (due jobs, retries, token care).
    publish_bot.start_worker(publishing)

    logger.info("VibeOS is running.")
    app.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
