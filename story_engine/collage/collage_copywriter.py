"""Short Story line. A verified, relevant trend is passed to the model; otherwise a safe evergreen line."""
import os, json, requests

_FALLBACK = {"streetwear_baddie": "out here, in our own lane", "kpop_editorial": "the moment said everything",
             "photo_dump": "camera roll did its thing", "scrapbook_chaos": "little moments, loud memories",
             "classy_editorial": "a little moment worth keeping", "playful_hype": "okay but THIS era??"}


def _clean_tags(tags):
    out = []
    for t in (tags or [])[:3]:
        t = "".join(ch for ch in str(t) if ch.isalnum() or ch == "_")
        if t: out.append("#" + t)
    return out


def write_copy(category, theme, trend):
    key = os.getenv("GROQ_API_KEY_COLLAGE") or os.getenv("GROQ_API_KEY_3")
    term = trend.get("term") if trend.get("verified") else None
    if key:
        try:
            model = os.getenv("COLLAGE_COPY_MODEL", "llama-3.3-70b-versatile")
            hint = (f"A verified trending topic right now: \"{term}\". Reference it ONLY if it fits the photos' vibe "
                    "naturally; never state facts about it."
                    if term else "No trend available; do not mention any trend or news.")
            prompt = ("Write one natural Instagram Story thought, max 8 words, plus <=3 relevant hashtags. "
                      "Lowercase/playful punctuation is fine. Return JSON {\"text\":..., \"hashtags\":[...]}. "
                      f"Photo category={category}, visual style={theme}. {hint}")
            r = requests.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": "Bearer " + key},
                              json={"model": model, "temperature": 0.8, "max_tokens": 90,
                                    "messages": [{"role": "user", "content": prompt}]}, timeout=18)
            r.raise_for_status()
            t = r.json()["choices"][0]["message"]["content"]; a, b = t.find("{"), t.rfind("}")
            d = json.loads(t[a:b + 1]); text = str(d.get("text", "")).strip()[:100]
            if text:
                return {"text": text, "hashtags": _clean_tags(d.get("hashtags")), "used_trend": bool(term)}
        except Exception:
            pass
    return {"text": _FALLBACK.get(theme, _FALLBACK["classy_editorial"]), "hashtags": [], "used_trend": False}
