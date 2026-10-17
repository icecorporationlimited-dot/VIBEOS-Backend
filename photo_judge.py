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
    "noisy": 5,
    "eyes_closed": 8,          # people shots: blink / closed eyes
    "awkward_expression": 8,   # mid-speech, grimace, unflattering frame
    "face_blurry": 12,         # background sharp but the face is soft
    # Autonomous-posting safety: these two are BLOCKERS, not ordinary penalties.
    "sensitive_info": 40,      # ID/card/document/screen with private data, plate, address
    "unsafe_content": 100,     # nudity, graphic violence, hate symbols, etc.
}
BLOCK_FLAGS = {"sensitive_info", "unsafe_content"}
BLOCK_SCORE_CAP = 20          # a blocked photo can never reach the auto-post bar
MAX_PENALTY = 35              # applies to ordinary flags only
LOCAL_BLEND = 0.30            # share of the local pixel analysis in composition / color
DISAGREE_GAP = 15             # two passes this far apart trigger a third (median of 3)

QUALITY_BLEND = 0.4          # share of the pixel-based quality score in "technical"
RECHECK_RANGE = (50, 60)     # scores this close to the 55 bar are scored twice
MIN_STYLE_CONFIDENCE = 50    # below this the style falls back to "natural"

# What each editing style is for. Only styles that exist in editor.STYLES are shown.
STYLE_GUIDE = {
    "natural": "balanced everyday daylight, true-to-life colours, authentic candid look; the safe default",
    "baddie": "night / flash / glam, bold fashion or confident portraits, strong contrast and punchy colour",
    "soft": "pastel, airy, bright and gentle light, florals, dreamy low-contrast look",
    "moody": "dark, low-key, cinematic; rain, night, deep shadows, atmosphere over brightness",
    "golden": "sunrise / sunset / golden-hour warmth, glowing backlight, warm skin and sky tones",
    "clean": "bright, minimal, neutral or white backgrounds, cafe/skincare/product/interior polish",
    "film": "nostalgic analog feel: street, travel, candid or vintage-looking scenes with grain",
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
def describe_local(local: dict | None) -> str:
    """Turn the local analysis into short factual hints for the vision model."""
    if not local:
        return ""
    bits = []
    fc = local.get("face_count")
    if fc is not None:
        bits.append(f"{fc} face(s) detected by a basic detector (may be wrong)")
        faces = local.get("faces") or []
        if faces:
            f0 = faces[0]
            bits.append(f"main face covers {round(100 * f0.get('area', 0))}% of the frame"
                        + (", touches the frame edge" if f0.get("cut_off") else ""))
    sub = local.get("subject_sharpness")
    if sub is not None:
        bits.append("measured focus on the main subject is "
                    + ("very soft" if sub < 38 else "soft" if sub < 90
                       else "good" if sub < 200 else "very crisp"))
    if local.get("motion_blur"):
        bits.append("directional (motion) blur suspected")
    if (local.get("noise") or 0) > 4:
        bits.append("visible sensor noise")
    if (local.get("screenshot_likelihood") or 0) >= 0.5:
        bits.append("flat UI-like pixels: may be a screenshot")
    if not bits:
        return ""
    return ("MEASURED FACTS (from pixel analysis; treat as hints and verify against the image):\n- "
            + "\n- ".join(bits) + "\n")


def build_prompt(styles, caption_styles=None, vibe_hint: str | None = None,
                 facts: str | None = None) -> str:
    style_names = [s for s in styles]
    style_lines = "\n".join(
        f'- "{s}": {STYLE_GUIDE.get(s, "a distinct look; use only when it clearly fits")}'
        for s in style_names)
    caption_names = list(caption_styles) if caption_styles else style_names
    caption_lines = "\n".join(
        f'- "{c}": {CAPTION_MODE_GUIDE[c]}' for c in caption_names if c in CAPTION_MODE_GUIDE)
    hint = (f'The creator prefers the "{vibe_hint}" look. Respect it only if the photo '
            "genuinely suits it; never force it.\n") if vibe_hint else ""
    flags = ", ".join(FLAG_PENALTIES)
    facts = facts or ""
    return f"""You are the photo editor of a top Instagram creator agency. Judge ONE photo for
posting on Instagram (feed 4:5 and Story 9:16) and choose the best editing style for it.

Look at the image carefully before answering. Do not identify the person, infer age,
ethnicity, religion or other sensitive traits, and never judge body shape or attractiveness.
Judge photography, not people.

{facts}SCORING (each 0-100, be strict and calibrated):
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
Return an empty list when none apply. Safety flags matter most because posts are published
automatically with nobody reviewing them: use "unsafe_content" for nudity/sexual content,
graphic violence or hateful symbols, and "sensitive_info" when private data is readable
(ID or bank card, document, phone/computer screen with personal details, license plate,
home address, boarding pass). Use "eyes_closed", "awkward_expression" or "face_blurry" only
for people shots.

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
def compute_score(data: dict, quality_score=None, local: dict | None = None) -> dict:
    """Turn the model's sub-scores + flags into the final score (never trusts a model total).

    quality_score: the pixel-based 0-100 score (blended into "technical").
    local: photo_intel.deep_analyze() output; its composition / colour scores are blended
    in (so one hallucinated sub-score cannot decide a post) and its hard evidence
    (blur, screenshot) is added to the flags the model reported.
    """
    subs = {k: _clamp(data.get(k, 0)) for k in WEIGHTS}
    if isinstance(quality_score, (int, float)) and not isinstance(quality_score, bool):
        q = _clamp(quality_score)
        subs["technical"] = _clamp(
            subs["technical"] * (1 - QUALITY_BLEND) + q * QUALITY_BLEND)
    local_scores = (local or {}).get("scores") or {}
    for key in ("composition", "color"):
        if key in local_scores:
            subs[key] = _clamp(subs[key] * (1 - LOCAL_BLEND) + _clamp(local_scores[key]) * LOCAL_BLEND)

    raw_flags = data.get("flags") or []
    if isinstance(raw_flags, str):
        raw_flags = [raw_flags]
    flags = []
    for f in raw_flags:
        name = str(f).strip().lower().replace(" ", "_").replace("-", "_")
        if name in FLAG_PENALTIES and name not in flags:
            flags.append(name)
    # Pixel evidence that is more reliable than a vision model's eyeballing.
    if local:
        if (local.get("screenshot_likelihood") or 0) >= 0.7 \
                and "screenshot_or_screen_photo" not in flags:
            flags.append("screenshot_or_screen_photo")
        if "blurry" in (local.get("flags") or []) and "blurry" not in flags \
                and (local.get("subject_sharpness") or 999) < 25:
            flags.append("blurry")

    blocked = [f for f in flags if f in BLOCK_FLAGS]
    penalty = min(MAX_PENALTY, sum(FLAG_PENALTIES[f] for f in flags if f not in BLOCK_FLAGS))

    weighted = sum(subs[k] * w for k, w in WEIGHTS.items())
    score = _clamp(weighted - penalty)
    if blocked:
        score = min(score, BLOCK_SCORE_CAP)
    return {"score": score, "scores": subs, "flags": flags, "penalty": penalty,
            "blocked": blocked}


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


def _merge(results: list[dict]) -> dict:
    """Average several scoring passes; keep a flag only when most passes agree
    (safety flags stick if ANY pass raised them)."""
    base = dict(results[0])
    k = len(results)
    scores = sorted(r["score"] for r in results)
    base["score"] = _clamp(scores[k // 2] if k % 2 else (scores[k // 2 - 1] + scores[k // 2]) / 2)
    base["scores"] = {key: _clamp(sum(r["scores"][key] for r in results) / k) for key in WEIGHTS}
    need = k // 2 + 1
    flags = []
    for f in FLAG_PENALTIES:
        votes = sum(f in r["flags"] for r in results)
        if votes >= need or (f in BLOCK_FLAGS and votes >= 1) \
                or (FLAG_PENALTIES[f] >= 15 and votes >= 1 and f not in BLOCK_FLAGS and k == 2):
            flags.append(f)
    base["flags"] = flags
    base["blocked"] = [f for f in flags if f in BLOCK_FLAGS]
    if base["blocked"]:
        base["score"] = min(base["score"], BLOCK_SCORE_CAP)
    return base


def judge_photo(vision: Callable[[str, str, float], dict], image_b64: str, *,
                styles, caption_styles=None, vibe_hint: str | None = None,
                quality_score=None, recheck=RECHECK_RANGE, local: dict | None = None) -> dict:
    """Score the photo and pick its style. Raises if the model returns no usable answer.

    local: optional photo_intel analysis. It grounds the model with measured facts,
    is blended into the sub-scores, and decides nothing on its own.
    Close-to-the-bar scores are judged twice; if the two passes disagree by
    DISAGREE_GAP or more a third pass is taken and the median wins.
    """
    prompt = build_prompt(styles, caption_styles, vibe_hint, describe_local(local))
    first = vision(prompt, image_b64, 0.2)
    result = compute_score(first, quality_score, local)
    result.update(choose_style(first, styles, caption_styles, vibe_hint or "natural"))
    result["reason"] = str(first.get("reason") or "")[:240]
    result["strengths"] = str(first.get("strengths") or "")[:200]
    result["weaknesses"] = str(first.get("weaknesses") or "")[:200]
    result["rechecked"] = False
    result["passes"] = 1

    near_bar = recheck and recheck[0] <= result["score"] <= recheck[1]
    if near_bar or result["blocked"]:        # a safety flag is never trusted to one pass
        passes = [dict(result)]
        try:
            second = vision(prompt, image_b64, 0.4)
            passes.append(compute_score(second, quality_score, local))
            if abs(passes[0]["score"] - passes[1]["score"]) >= DISAGREE_GAP:
                third = vision(prompt, image_b64, 0.3)
                passes.append(compute_score(third, quality_score, local))
            merged = _merge(passes)
            for key in ("score", "scores", "flags", "penalty", "blocked"):
                result[key] = merged[key]
            result["rechecked"] = True
            result["passes"] = len(passes)
        except Exception:
            logger.warning("Extra scoring pass failed; keeping first score", exc_info=True)
    return result
