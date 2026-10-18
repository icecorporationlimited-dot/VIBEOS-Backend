
"""Hosted pipeline for browser uploads: buffer -> analyse -> edit + caption -> ready.

This mirrors the Telegram full-auto path but is keyed by a verified web
Principal. It never publishes anything itself: it only prepares ready assets.
"""
import base64
import io
import json
import logging
import queue
import re
import threading
from datetime import datetime, timezone

from tagger_client import tag_photo

import copywriter
import photo_judge
from content_buffer import ANALYZED, READY, SELECTED, UPLOADED
from publishing.identity import Principal

logger = logging.getLogger(__name__)

MIN_AUTO_SCORE = 55
MAX_ATTEMPTS = 3
AI_IMAGE_EDGE = 1024
DEFAULT_VIBE = "natural"


def _now():
    return datetime.now(timezone.utc)


def _parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("No JSON object found in AI response")
    return json.loads(text[start:end + 1])


def _clean_overlay(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'")
    text = "".join(ch for ch in text if ord(ch) < 0x2070)
    return " ".join(text.split()[:7])[:60]


def _small_jpeg(data):
    """Downscaled JPEG bytes for the vision model."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(
        Image.open(io.BytesIO(data))
    ).convert("RGB")
    img.thumbnail((AI_IMAGE_EDGE, AI_IMAGE_EDGE))

    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


class WebPipeline:
    """Turns one selected asset into a ready, captioned, edited asset."""

    def __init__(
        self,
        buffer,
        groq_factory,
        model,
        language="english",
        edit_photo=None,
        styles=None,
        occasion_hint=None,
    ):
        self.buffer = buffer
        self._groq_factory = groq_factory
        self._groq = None
        self.model = model
        self.language = language
        self.occasion_hint = occasion_hint

        # Production rendering is handled by worker.py.
        if styles is None:
            from media_names import STYLES
            styles = STYLES

        self.edit_photo = edit_photo
        self.styles = set(styles)

    # ---- AI calls ---------------------------------------------------------

    def _client(self):
        if self._groq is None:
            self._groq = self._groq_factory()
        return self._groq

    def _vision(self, prompt, image_b64, temperature):
        resp = self._client().chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64," + image_b64
                            },
                        },
                    ],
                }
            ],
            temperature=temperature,
        )
        return _parse_json(resp.choices[0].message.content)

    def score_and_style(self, image_b64, vibe=None, quality_score=None):
        """Score the photo and choose an edit style in one vision call."""
        try:
            return photo_judge.judge_photo(
                self._vision,
                image_b64,
                styles=sorted(self.styles),
                caption_styles=list(copywriter.CAPTION_STYLES),
                vibe_hint=vibe,
                quality_score=quality_score,
            )
        except Exception:
            logger.exception("Web photo scoring failed")
            raise

    def caption(self, image_b64, vibe, recent, mode=None, context=None):
        mode = (
            mode
            if mode in copywriter.CAPTION_STYLES
            else (
                vibe
                if vibe in copywriter.CAPTION_STYLES
                else "natural"
            )
        )

        prompt = copywriter.build_prompt(
            mode=mode,
            vibe=vibe,
            language=self.language,
            recent_captions=recent,
            trend_context=context,
        )

        last = None
        for attempt in range(2):
            try:
                raw = self._vision(
                    prompt,
                    image_b64,
                    0.9 if attempt == 0 else 0.6,
                )
                parsed = copywriter.parse_copy(raw, mode=mode)
                parsed["overlay"] = _clean_overlay(
                    raw.get("overlay", "")
                )
                return parsed
            except Exception as exc:
                last = exc
                logger.warning(
                    "Web caption attempt %s failed: %s",
                    attempt + 1,
                    exc,
                )

        raise ValueError(f"AI caption failed: {last}")

    # ---- One asset --------------------------------------------------------

    def _recent_captions(self, principal, limit=8):
        rows = self.buffer.list_assets(principal, status=READY)
        captions = [
            (a.get("creative") or {}).get("caption") or ""
            for a in rows[-limit:]
        ]
        return [caption for caption in captions if caption]

    def note(self, asset_id, **fields):
        self.buffer.assets.update_one(
            {"_id": asset_id},
            {"$set": fields},
        )

    def prepare(self, principal, asset_id):
        """Prepare one selected asset. Returns ready or skipped."""
        data = self.buffer.get_media_for(principal, asset_id)
        image_b64 = base64.b64encode(_small_jpeg(data)).decode()

        asset = self.buffer.assets.find_one({"_id": asset_id}) or {}
        quality = (asset.get("analysis") or {}).get("quality_score")

        verdict = self.score_and_style(
            image_b64,
            quality_score=quality,
        )

        # Optional tagger style override.
        # A tagger failure is logged; the AI-selected style remains available.
        try:
            tag = tag_photo(data)
            if tag and not tag.get("low_confidence", True):
                from media_names import resolve_style
                tag_style = resolve_style(tag["style"], "natural")   # glow->golden, soft->film ...
                verdict["style"] = tag_style
                verdict["style_confidence"] = tag["style_confidence"]
                verdict["style_reason"] = "tagger: " + tag["style_label"]
                verdict["caption_mode"] = (
                    tag_style
                    if tag_style != "mono"
                    else "natural"
                )
        except Exception:
            logger.exception(
                "Photo tagger failed; using AI-selected style"
            )

        detail = dict(
            web_score=verdict["score"],
            web_scores=verdict["scores"],
            web_flags=verdict["flags"],
            web_style=verdict["style"],
            web_style_confidence=verdict["style_confidence"],
            web_style_reason=verdict["style_reason"],
            web_reason=verdict["reason"],
        )

        if verdict["score"] < MIN_AUTO_SCORE:
            self.buffer.release(asset_id)
            self.note(
                asset_id,
                web_note="low_score",
                updated_at=_now(),
                **detail,
            )
            return "skipped"

        style = verdict["style"]
        context = None

        if self.occasion_hint is not None:
            try:
                context = self.occasion_hint(principal, asset)
            except Exception:
                logger.exception(
                    "Occasion hint failed; writing a normal caption"
                )

        copy = self.caption(
            image_b64,
            style,
            self._recent_captions(principal),
            mode=verdict["caption_mode"],
            context=context,
        )

        overlay = copy.get("overlay") or None

        if self.edit_photo is None:
            self.buffer.prepare_ready_job(
                asset_id,
                copy["caption"],
                copy["hashtags"],
                style,
                ("post", "story"),
                style=style,
                overlay=overlay,
                text_style="minimal",
            )
        else:
            # Injected local editor, mainly for tests.
            def _edit(raw, _vibe, size):
                return self.edit_photo(
                    raw,
                    style,
                    size,
                    overlay,
                    "minimal",
                )

            self.buffer.prepare_ready(
                asset_id,
                copy["caption"],
                copy["hashtags"],
                style,
                ("post", "story"),
                _edit,
            )

        self.note(
            asset_id,
            web_note=None,
            **detail,
        )
        return "ready"


class WebIngest:
    """Background thread that prepares browser uploads in batches."""

    def __init__(self, buffer, autopilot, pipeline):
        self.buffer = buffer
        self.autopilot = autopilot
        self.pipeline = pipeline
        self._q = queue.Queue()
        self._queued = set()
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    # ---- Lifecycle --------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="vibeos-web-ingest",
            daemon=True,
        )
        self._thread.start()
        self.recover()

    def stop(self, timeout=10):
        self._stop.set()
        self._q.put(None)

        if self._thread:
            self._thread.join(timeout)

    # ---- Submitting -------------------------------------------------------

    def submit(self, principal, batch_id):
        key = (principal.owner_id, batch_id)

        with self._lock:
            if key in self._queued:
                return
            self._queued.add(key)

        self._q.put(key)

    def submit_pending(self, principal):
        """Queue unfinished batches for an owner."""
        for batch_id in self._pending_batches(principal):
            self.submit(principal, batch_id)

    def recover(self):
        """Re-queue web batches that did not reach ready before restart."""
        try:
            owners = {
                asset.get("owner_id")
                for asset in self.buffer.assets.find(
                    {
                        "owner_id": {"$regex": "^web:"},
                        "status": {
                            "$in": [UPLOADED, ANALYZED, SELECTED]
                        },
                    }
                )
            }
        except Exception:
            logger.exception("Web ingest recovery scan failed")
            return

        for owner in filter(None, owners):
            try:
                principal = Principal.from_web(owner[len("web:"):])
            except Exception:
                continue

            self.submit_pending(principal)

    def _pending_batches(self, principal):
        batches = []

        for status in (UPLOADED, ANALYZED, SELECTED):
            for asset in self.buffer.list_assets(
                principal,
                status=status,
            ):
                if asset.get("web_note") in ("low_score", "failed"):
                    continue

                batch_id = asset.get("batch_id")
                if batch_id and batch_id not in batches:
                    batches.append(batch_id)

        return batches

    # ---- Worker -----------------------------------------------------------

    def _loop(self):
        while not self._stop.is_set():
            item = self._q.get()

            if item is None:
                break

            owner_id, batch_id = item

            with self._lock:
                self._queued.discard(item)

            try:
                principal = Principal.from_web(
                    owner_id[len("web:"):]
                )
                self.process_batch(principal, batch_id)
            except Exception:
                logger.exception("Web ingest batch failed")

    def _active(self, principal):
        state = self.autopilot.get(principal)
        return bool(
            state.get("enabled")
            and state.get("full_auto")
            and not state.get("paused")
        )

    def process_batch(self, principal, batch_id):
        """Analyse, shortlist, then prepare each selected photo."""
        if not self._active(principal):
            return {"skipped": "autopilot off"}

        self.buffer.analyze_batch(principal, batch_id)

        count = len(
            self.buffer.list_assets(
                principal,
                batch_id=batch_id,
            )
        ) or 1

        self.buffer.select_shortlist(
            principal,
            batch_id,
            n=count,
        )

        out = {
            "ready": 0,
            "skipped": 0,
            "failed": 0,
        }

        for asset in self.buffer.list_assets(
            principal,
            status=SELECTED,
            batch_id=batch_id,
        ):
            if not self._active(principal):
                self.buffer.release(asset["_id"])
                continue

            try:
                result = self.pipeline.prepare(
                    principal,
                    asset["_id"],
                )
                out[result] += 1

            except Exception:
                logger.exception(
                    "Web prepare failed for %s",
                    asset["_id"],
                )
                out["failed"] += 1

                attempts = int(asset.get("web_attempts") or 0) + 1
                fields = {"web_attempts": attempts}

                if attempts >= MAX_ATTEMPTS:
                    fields["web_note"] = "failed"

                self.pipeline.note(
                    asset["_id"],
                    **fields,
                )

                if attempts >= MAX_ATTEMPTS:
                    self.buffer.release(asset["_id"])
                else:
                    # Retry after a minute rather than spinning.
                    timer = threading.Timer(
                        60.0,
                        self.submit,
                        (principal, batch_id),
                    )
                    timer.daemon = True
                    timer.start()

        return out
