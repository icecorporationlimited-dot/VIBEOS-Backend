"""photo_judge.py - advanced AI photo scoring + AI style selection (pure, no DB/network).

One vision call returns BOTH:
  * seven rubric sub-scores (0-100) and a list of technical/risk flags, and
  * the editing style (filter) and caption mode that best fit the photo.

The final score is computed HERE from the sub-scores (weighted, with flag
penalties and an optional blend with the cheap pixel-based quality score), never
taken from the model. This keeps scores consistent from run to run and makes the
55-point auto-post bar meaningful. Photos that land close to the bar are scored
a second time and averaged, so one noisy answer cannot decide a post.

The caller supplies `vision(prompt, image_b64, temperature) -> dict`, so this
module works with any provider and is trivial to unit test.
"""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Rubric
# --------------------------------------------------------------------------
WEIGHTS = {
    "composition": 0.18,   # framing, subject placement, balance, crop-safety
    "light": 0.18,         # quality + direction of light, exposure, dynamic range
    "subject": 0.18,       # expression/pose naturalness, or interest of the main subject
    "background": 0.12,    # clean or interesting, no distractions
    "color": 0.12,         # palette harmony, grading potential
    "technical": 0.12,     # focus, sharpness, noise, resolution
    "scroll_stop": 0.10,   # distinctiveness, emotion, story: would a stranger stop?
}

# Points removed from the weighted score for each flag the model reports.
FLAG_PENALTIES = {
    "blurry": 15,
    "motion_blur": 8,
    "too_dark": 10,
    "overexposed": 10,
    "subject_cut_off": 8,
    "cluttered_background": 6,
    "text_or_watermark": 6,
    "screenshot_or_screen_photo": 25,
    "heavy_filter_already": 4,
    "low_res_looking": 8,
}
MAX_PENALTY = 35

QUALITY_BLEND = 0.4          # share of the pixel-based quality score in "technical"
RECHECK_RANGE = (50, 60)     # scores this close to the 55 bar are scored twice
MIN_STYLE_CONFIDENCE = 50    # below this the style falls back to "natural"

# What each editing style is for. Only styles that exist in editor.STYLES are shown.
STYLE_GUIDE = {
    "natural": "let the engine decide; use only if no look clearly fits",
    "y2k": "Y2K digicam / direct flash: night out, parties, friend groups, streetwear selfies; bold, candid, dark background",
    "moody": "cinematic moody: streetwear, travel, architecture, dramatic portraits; deep detailed shadows, background separation",
    "film": "soft film / warm vintage: cafe, travel dumps, candid moments; relaxed, nostalgic, muted, fine grain",
    "clean": "clean editorial: outfit/fashion, creator profile, brand work, casual daylight selfies; neutral, accurate colour, crisp",
    "golden": "golden hour glow: outdoor portraits, sunsets, warm backlight; dreamy, warm without an orange cast",
    "mono": "monochrome B&W: expressive portraits, street, strong light/shape/composition where colour is not the point",
}

CAPTION_MODE_GUIDE = {
    "funny": "ONLY when the photo is clearly playful, goofy or relatable (a joke is obvious)",
}


def _clamp(value, lo=0, hi=100) -> int:
    try:
        return int(max(lo, min(hi, round(float(value)))))
    except (TypeError, ValueError):
        return lo


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------
def build_prompt(styles, caption_styles=None, vibe_hint: str | None = None) -> str:
    style_names = [s for s in styles if s != "natural"]      # "natural" = auto, never a judge pick
    style_lines = "\n".join(
        f'- "{s}": {STYLE_GUIDE.get(s, "a distinct look; use only when it clearly fits")}'
        for s in style_names)
    caption_names = list(caption_styles) if caption_styles else style_names
    caption_lines = "\n".join(
        f'- "{c}": {CAPTION_MODE_GUIDE[c]}' for c in caption_names if c in CAPTION_MODE_GUIDE)
    hint = (f'The creator prefers the "{vibe_hint}" look. Respect it only if the photo '
            "genuinely suits it; never force it.\n") if vibe_hint else ""
    flags = ", ".join(FLAG_PENALTIES)
    return f"""You are the photo editor of a top Instagram creator agency. Judge ONE photo for
posting on Instagram (feed 4:5 and Story 9:16) and choose the best editing style for it.

Look at the image carefully before answering. Do not identify the person, infer age,
ethnicity, religion or other sensitive traits, and never judge body shape or attractiveness.
Judge photography, not people.

SCORING (each 0-100, be strict and calibrated):
- 50 = an average, forgettable phone snapshot
- 65 = good, clearly postable
- 80 = strong, would perform above the creator's average
- 90+ = exceptional and rare. Do not hand out 85+ easily.
Use the full range. Most photos should fall between 35 and 75.

Sub-scores:
- composition: framing, subject placement, balance, leading lines, and whether it survives
  cropping to 4:5 and 9:16 without cutting the subject
- light: quality and direction of light, correct exposure, dynamic range, no harsh clipping
- subject: for people, natural expression and pose; otherwise how interesting the main subject is
- background: clean or purposefully interesting; penalise clutter and distractions
- color: palette harmony and how well the colours can be graded
- technical: focus on the subject, sharpness, noise, apparent resolution
- scroll_stop: distinctiveness, emotion, story; would a stranger stop scrolling?

FLAGS: list every flag that clearly applies, using ONLY these names: {flags}.
Return an empty list when none apply.

STYLE CHOICE: pick the single best editing style for THIS photo from:
{style_lines}
Decide from the light, colour palette, mood and scene, in this order: (1) scene/time of day,
(2) existing tones and mood, (3) subject type. Give filter_confidence 0-100 (how sure you are
this style beats the others) and the runner_up style.
{hint}
CAPTION MODE: usually the same as the chosen style.
{caption_lines or "Use the chosen style."}

Return ONLY valid JSON, no markdown, exactly this shape:
{{
  "composition": 0, "light": 0, "subject": 0, "background": 0,
  "color": 0, "technical": 0, "scroll_stop": 0,
  "flags": [],
  "strengths": "short",
  "weaknesses": "short",
  "filter": "natural",
  "filter_confidence": 0,
  "runner_up": "natural",
  "filter_reason": "short",
  "caption_mode": "natural",
  "reason": "one short sentence explaining the score"
}}"""


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def compute_score(data: dict, quality_score=None) -> dict:
    """Turn the model's sub-scores + flags into the final score (never trusts a model total)."""
    subs = {k: _clamp(data.get(k, 0)) for k in WEIGHTS}
    if isinstance(quality_score, (int, float)) and not isinstance(quality_score, bool):
        q = _clamp(quality_score)
        subs["technical"] = _clamp(
            subs["technical"] * (1 - QUALITY_BLEND) + q * QUALITY_BLEND)

    raw_flags = data.get("flags") or []
    if isinstance(raw_flags, str):
        raw_flags = [raw_flags]
    flags = []
    for f in raw_flags:
        name = str(f).strip().lower().replace(" ", "_").replace("-", "_")
        if name in FLAG_PENALTIES and name not in flags:
            flags.append(name)
    penalty = min(MAX_PENALTY, sum(FLAG_PENALTIES[f] for f in flags))

    weighted = sum(subs[k] * w for k, w in WEIGHTS.items())
    return {"score": _clamp(weighted - penalty), "scores": subs, "flags": flags,
            "penalty": penalty}


def choose_style(data: dict, styles, caption_styles=None, fallback="natural") -> dict:
    """Validate the model's style choice; low confidence or unknown names fall back safely."""
    allowed = list(styles)
    base = fallback if fallback in allowed else (allowed[0] if allowed else "natural")

    def _norm(v):
        v = str(v or "").strip().lower()
        return v if v in allowed else None

    style = _norm(data.get("filter"))
    conf = _clamp(data.get("filter_confidence", 0))
    runner = _norm(data.get("runner_up"))
    if style is None or conf < MIN_STYLE_CONFIDENCE:
        style = base
    cap_allowed = list(caption_styles) if caption_styles else allowed
    mode = str(data.get("caption_mode") or "").strip().lower()
    if mode not in cap_allowed or (mode == "funny" and conf < 60):
        mode = style if style in cap_allowed else ("natural" if "natural" in cap_allowed else cap_allowed[0])
    return {"style": style, "style_confidence": conf, "runner_up": runner,
            "caption_mode": mode,
            "style_reason": str(data.get("filter_reason") or "")[:200]}


def judge_photo(vision: Callable[[str, str, float], dict], image_b64: str, *,
                styles, caption_styles=None, vibe_hint: str | None = None,
                quality_score=None, recheck=RECHECK_RANGE) -> dict:
    """Score the photo and pick its style. Raises if the model returns no usable answer."""
    prompt = build_prompt(styles, caption_styles, vibe_hint)
    first = vision(prompt, image_b64, 0.2)
    result = compute_score(first, quality_score)
    result.update(choose_style(first, styles, caption_styles, vibe_hint or "natural"))
    result["reason"] = str(first.get("reason") or "")[:240]
    result["strengths"] = str(first.get("strengths") or "")[:200]
    result["weaknesses"] = str(first.get("weaknesses") or "")[:200]
    result["rechecked"] = False

    if recheck and recheck[0] <= result["score"] <= recheck[1]:
        try:
            second = vision(prompt, image_b64, 0.4)
            again = compute_score(second, quality_score)
            result["score"] = _clamp((result["score"] + again["score"]) / 2)
            result["scores"] = {k: _clamp((result["scores"][k] + again["scores"][k]) / 2)
                                for k in WEIGHTS}
            result["flags"] = [f for f in result["flags"] if f in again["flags"]] or \
                [f for f in result["flags"] if FLAG_PENALTIES[f] >= 15]
            result["rechecked"] = True
        except Exception:
            logger.warning("Second scoring pass failed; keeping first score", exc_info=True)
    return result
