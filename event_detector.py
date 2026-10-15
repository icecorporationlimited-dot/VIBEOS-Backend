"""AI visual event classifier for VibeOS.

This module identifies likely content occasions from an image. It does not
assume a festival date: event dates must be present in event_calendar.json.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any


def _json_from_text(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise ValueError("AI response did not contain JSON")
    data = json.loads(match.group(0))
    confidence = float(data.get("confidence", 0))
    data["confidence"] = max(0.0, min(1.0, confidence))
    data["event_related"] = bool(data.get("event_related", False))
    data["event_key"] = str(data.get("event_key", "")).strip().lower().replace(" ", "_")[:80]
    data["content_type"] = str(data.get("content_type", "general"))[:80]
    data["visual_summary"] = str(data.get("visual_summary", ""))[:500]
    data["suggested_caption_angle"] = str(data.get("suggested_caption_angle", ""))[:300]
    return data


def classify_image(image_bytes: bytes, *, api_key: str, model: str = "qwen/qwen3.8-27b") -> dict[str, Any]:
    """Classify one image using a vision-capable Groq model.

    Returns conservative structured labels. On API/model failure, the caller
    should keep the asset in the normal queue rather than inventing an event.
    """
    if not api_key or str(api_key).startswith("your_"):
        raise ValueError("Set GROQ_API_KEY in settings.py before enabling AI event detection")
    from groq import Groq

    client = Groq(api_key=api_key)
    prompt = """You are a careful social media content planner. Inspect the image only.
Return ONLY valid JSON with these keys:
{
  "event_related": false,
  "event_key": "",
  "content_type": "general",
  "confidence": 0.0,
  "visual_summary": "short visible description",
  "suggested_caption_angle": "short creative direction"
}
Rules:
- event_related is true only when visible evidence strongly suggests a specific occasion (e.g. Diwali lamps/decor, birthday cake, graduation gown, wedding ceremony, Christmas tree).
- event_key should be a short canonical key such as diwali, birthday, wedding, graduation, christmas, new_year. If uncertain, use empty string and false.
- Do not infer a person's identity, religion, ethnicity, age, or other sensitive traits. Do not infer an event from the person's appearance alone.
- confidence must be 0 to 1. Use conservative confidence; if uncertain, use false.
- content_type must be exactly one of: general, occasion, behind_the_scenes, casual_update, daily_life. Use behind_the_scenes/casual_update/daily_life only for clearly candid, unposed snapshots; use general when unsure.
- Do not claim that an audio/song is trending or licensed.
"""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded}},
        ]}],
        temperature=0.1,
    )
    return _json_from_text(response.choices[0].message.content)
