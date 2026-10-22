"""Vision model for step 9 (does this photo fit the headline?).

build() -> callable `vision(prompt, image_b64, temperature) -> dict`, or None when no key is configured
(quality.py then keeps photo stories pending instead of silently rejecting them).

Plain `requests` to an OpenAI-compatible chat endpoint, so the pipeline image needs no extra SDK
(requirements-worker.txt has no `groq`).  Provider order (STORY_VISION_PROVIDER = auto | openrouter | groq):
  openrouter  OPENROUTER_API_KEY + OPENROUTER_VISION_MODEL (default openai/gpt-4o-mini, which accepts images).
              Separate from OPENROUTER_MODEL on purpose: that one may be a text-only model.
  groq        GROQ_API_KEY + VISION_MODEL (same settings the web app already uses).
`auto` = openrouter when its key exists (the pipeline already has it), else groq.
The image is shrunk to <=768 px before sending: cheaper, faster, and enough to judge relevance.
Keys are never logged; errors carry only the exception type.
"""
import base64
import io
import json
import logging
import os
import re

log = logging.getLogger(__name__)

ENDPOINTS = {"openrouter": "https://openrouter.ai/api/v1/chat/completions",
             "groq": "https://api.groq.com/openai/v1/chat/completions"}
MAX_SIDE = 768
TIMEOUT = 30


def _conf(name, default=""):
    v = os.getenv(name)
    if v:
        return v.strip()
    try:
        import settings
        return str(getattr(settings, name, "") or default).strip()
    except Exception:
        return default


def _real(key) -> bool:
    return bool(key) and not key.startswith("your_")


def shrink(image_b64: str) -> str:
    """Re-encode as a small JPEG. If Pillow cannot read it, the original is sent unchanged."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(image_b64))).convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=80)
        return base64.b64encode(out.getvalue()).decode()
    except Exception:
        return image_b64


def parse_json(text) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b < a:
        raise ValueError("no JSON object in vision reply")
    data = json.loads(text[a:b + 1])
    if not isinstance(data, dict):
        raise ValueError("vision reply is not an object")
    return data


def choose(env=None):
    """-> (provider, key, model) or None."""
    get = (lambda n, d="": (env.get(n) or d).strip()) if env is not None else _conf
    want = get("STORY_VISION_PROVIDER", "auto").lower()
    or_key, groq_key = get("OPENROUTER_API_KEY"), get("GROQ_API_KEY")
    if want in ("auto", "openrouter") and _real(or_key):
        return "openrouter", or_key, get("OPENROUTER_VISION_MODEL") or "openai/gpt-4o-mini"
    if want in ("auto", "groq") and _real(groq_key):
        return "groq", groq_key, get("VISION_MODEL") or "qwen/qwen3.8-27b"
    return None


def build(env=None, http=None):
    pick = choose(env)
    if pick is None:
        return None
    provider, key, model = pick
    if http is None:
        import requests as http

    def vision(prompt, image_b64, temperature=0):
        body = {"model": model, "temperature": temperature, "max_tokens": 200,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + shrink(image_b64)}}]}]}
        r = http.post(ENDPOINTS[provider], json=body, timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        r.raise_for_status()
        return parse_json(r.json()["choices"][0]["message"]["content"])

    vision.provider, vision.model = provider, model
    return vision
