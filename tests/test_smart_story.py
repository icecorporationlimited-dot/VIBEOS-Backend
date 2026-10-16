import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageStat

from creative_studio.story_themes import KINDS, render_story
from trends import photos, story_ai, verify
from trends.smart_story import build_story

NOW = datetime.now(timezone.utc)


def news(*pairs):
    return [{"title": t, "source": s, "url": "https://example.com/" + s.lower(), "snippet": ""} for s, t in pairs]


def item(title, headlines, traffic=50_000):
    return {"title": title, "news": news(*headlines), "approx_traffic_numeric": traffic,
            "expires_at": NOW + timedelta(hours=3), "evidence": {"region": "IN"}}


class FakeAI:
    ready = True

    def __init__(self, plan):
        self.plan = plan

    def chat(self, system, user, **kw):
        return "```json\n" + json.dumps(self.plan) + "\n```"


def photo_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (900, 1100), (90, 120, 160)).save(buf, "JPEG")
    return buf.getvalue()


CONGRATS = item("Neeraj Chopra gold", [("NDTV", "Neeraj Chopra wins gold medal"),
                                       ("HT", "Neeraj Chopra gold medal win thrills India")])
CONGRATS_PLAN = {"kind": "congrats", "sensitive": False, "confirmed": True, "confidence": 0.9,
                 "person": "Neeraj Chopra", "headline": "Neeraj ne rachaya itihaas \U0001F947",
                 "subline": "Poora India garv se jhoom utha", "chip": "CONGRATS", "emojis": ["\U0001F389"]}
LOSS = item("Veteran actor passes away", [("NDTV", "Veteran actor Ramesh Kumar passes away at 80"),
                                           ("HT", "Actor Ramesh Kumar passes away"),
                                           ("TOI", "Ramesh Kumar, veteran actor, passes away")])
LOSS_PLAN = {"kind": "solemn", "sensitive": False, "confirmed": True, "confidence": 0.95,
             "person": "Ramesh Kumar", "headline": "Ramesh Kumar nahi rahe",
             "subline": "Ek yug ka ant. Om Shanti.", "chip": "", "emojis": ["\U0001F54A\uFE0F"]}


class VerifyTests(unittest.TestCase):
    def test_good_item_passes_pre_check(self):
        self.assertTrue(verify.verdict(verify.pre_check(CONGRATS))["ok"])

    def test_single_publisher_fails(self):
        v = verify.verdict(verify.pre_check(item("Neeraj Chopra gold", [("NDTV", "Neeraj Chopra wins gold")])))
        self.assertFalse(v["ok"])
        self.assertTrue(v["reason"].startswith("sources"))

    def test_rumor_language_blocks_death_hoax(self):
        hoax = item("Actor Ramesh Kumar", [("A", "Ramesh Kumar death rumours spread"),
                                           ("B", "Ramesh Kumar alive, hoax says family")])
        self.assertIn("no_rumor", verify.verdict(verify.pre_check(hoax))["reason"])

    def test_hard_topics_stay_blocked(self):
        for head in ("Supreme Court verdict on Ramesh Kumar", "Ramesh Kumar arrested", "Ramesh Kumar election rally"):
            it = item("Ramesh Kumar", [("A", head), ("B", head + " report")])
            self.assertFalse(verify.verdict(verify.pre_check(it))["ok"], head)

    def test_death_words_are_not_hard_blocked(self):
        self.assertTrue(verify.verdict(verify.pre_check(LOSS))["ok"])

    def test_non_latin_title_still_cross_checked(self):
        it = item("विराट कोहली शतक", [("A", "विराट कोहली का शतक"), ("B", "विराट कोहली शतक जड़ा")])
        self.assertTrue(verify.verdict(verify.pre_check(it))["ok"])

    def test_solemn_needs_confirmation_and_three_publishers(self):
        two = item("Ramesh Kumar passes away", [("A", "Ramesh Kumar passes away"), ("B", "Ramesh Kumar passes away today")])
        plan = story_ai.sanitize_plan(LOSS_PLAN, two)
        steps = verify.post_check(two, plan)
        self.assertFalse(verify.verdict(steps)["ok"])
        self.assertTrue(verify.verdict(verify.post_check(LOSS, story_ai.sanitize_plan(LOSS_PLAN, LOSS)))["ok"])


class PlanSanitizerTests(unittest.TestCase):
    def test_devanagari_headline_rejected(self):
        bad = {**CONGRATS_PLAN, "headline": "नीरज ने रचा इतिहास"}
        self.assertIsNone(story_ai.sanitize_plan(bad, CONGRATS))

    def test_hashtags_and_links_removed(self):
        plan = story_ai.sanitize_plan({**CONGRATS_PLAN, "subline": "Garv hai #India https://x.co"}, CONGRATS)
        self.assertNotIn("#", plan["subline"])
        self.assertNotIn("http", plan["subline"])

    def test_person_not_in_sources_is_dropped(self):
        plan = story_ai.sanitize_plan({**CONGRATS_PLAN, "person": "Virat Kohli"}, CONGRATS)
        self.assertEqual(plan["person"], "")

    def test_solemn_rules(self):
        self.assertIsNone(story_ai.sanitize_plan({**LOSS_PLAN, "headline": "Legend gone \U0001F389"}, LOSS))
        self.assertIsNone(story_ai.sanitize_plan({**LOSS_PLAN, "subline": "Over 500 people died"}, LOSS))
        plan = story_ai.sanitize_plan({**LOSS_PLAN, "emojis": ["\U0001F525", "\U0001F64F"],
                                       "headline": "Ramesh Kumar nahi rahe!"}, LOSS)
        self.assertEqual(plan["emojis"], ["\U0001F64F"])
        self.assertNotIn("!", plan["headline"])

    def test_unknown_kind_rejected(self):
        self.assertIsNone(story_ai.sanitize_plan({**CONGRATS_PLAN, "kind": "politics"}, CONGRATS))

    def test_fallback_never_solemn(self):
        it = item("IPL final tonight", [("A", "IPL final tonight match preview"), ("B", "IPL final match tonight")])
        plan = story_ai.fallback_plan(it)
        self.assertEqual(plan["kind"], "sports")
        self.assertIsNone(story_ai.fallback_plan(LOSS))


class PhotoTests(unittest.TestCase):
    def test_license_filter(self):
        for ok in ("CC BY-SA 4.0", "CC BY 2.0", "CC0", "Public domain"):
            self.assertTrue(photos.license_ok(ok), ok)
        for bad in ("CC BY-NC 4.0", "CC BY-ND 2.0", "Fair use", "", "All rights reserved"):
            self.assertFalse(photos.license_ok(bad), bad)

    def _http(self, lic, mime="image/jpeg", title="Neeraj Chopra"):
        data = photo_bytes()

        class R:
            def __init__(s, payload=None, content=b""):
                s._p, s.content = payload, content

            def raise_for_status(s):
                pass

            def json(s):
                return s._p

        class Http:
            def get(s, url, params=None, headers=None, timeout=0):
                if "wikipedia.org" in url:
                    return R({"query": {"pages": {"1": {"title": title, "index": 1, "description": "Indian javelin thrower",
                                                         "pageimage": "Neeraj.jpg"}}}})
                if "commons" in url:
                    return R({"query": {"pages": {"9": {"imageinfo": [{
                        "mime": mime, "width": 1200, "height": 1500, "thumburl": "https://upload.wikimedia.org/x.jpg",
                        "extmetadata": {"LicenseShortName": {"value": lic},
                                        "Artist": {"value": "<a href='#'>Jane Doe</a>"}}}]}}}})
                return R(content=data)
        return Http()

    def test_free_licence_returns_photo_with_credit(self):
        got = photos.find_person_photo("Neeraj Chopra", http=self._http("CC BY-SA 4.0"))
        self.assertIn("Jane Doe", got["credit"])
        self.assertIn("CC BY-SA 4.0", got["credit"])

    def test_non_free_or_wrong_person_gives_none(self):
        self.assertIsNone(photos.find_person_photo("Neeraj Chopra", http=self._http("CC BY-NC 4.0")))
        self.assertIsNone(photos.find_person_photo("Neeraj Chopra", http=self._http("CC BY 4.0", title="Neeraj Kumar")))
        self.assertIsNone(photos.find_person_photo("Neeraj Chopra", http=self._http("CC BY 4.0", mime="image/gif")))


class RenderTests(unittest.TestCase):
    def test_every_kind_renders_1080x1920(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kind in KINDS:
                for photo in (None, photo_bytes()):
                    p = render_story({"kind": kind, "headline": "Test headline \U0001F525 hai", "subline": "Sub line"},
                                     Path(tmp) / f"{kind}.jpg", photo_bytes=photo)
                    self.assertEqual(Image.open(p).size, (1080, 1920))

    def test_solemn_is_black_not_colourful(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = render_story({"kind": "solemn", "headline": "Shraddhanjali"}, Path(tmp) / "s.jpg", photo_bytes=photo_bytes())
            im = Image.open(p).convert("RGB")
            self.assertLess(sum(im.getpixel((20, 20))), 30)
            r, g, b = ImageStat.Stat(im.crop((300, 500, 780, 900))).mean
            self.assertLess(max(r, g, b) - min(r, g, b), 12)     # black & white portrait

    def test_unknown_kind_or_missing_headline_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                render_story({"kind": "nope", "headline": "x"}, Path(tmp) / "a.jpg")
            with self.assertRaises(ValueError):
                render_story({"kind": "sports", "headline": ""}, Path(tmp) / "a.jpg")


class BuildStoryTests(unittest.TestCase):
    def test_congrats_with_licensed_photo(self):
        got = build_story(CONGRATS, ai=FakeAI(CONGRATS_PLAN),
                          photo_finder=lambda name: {"bytes": photo_bytes(), "credit": "Photo: Jane / CC BY 4.0 / Wikimedia"})
        self.assertTrue(got["ok"], got["reason"])
        self.assertEqual(got["plan"]["person"], "Neeraj Chopra")
        self.assertTrue(got["jpg"][:2] == b"\xff\xd8")

    def test_loss_becomes_solemn(self):
        got = build_story(LOSS, ai=FakeAI(LOSS_PLAN), photo_finder=lambda n: None)
        self.assertTrue(got["ok"], got["reason"])
        self.assertEqual(got["plan"]["kind"], "solemn")

    def test_loss_blocked_when_solemn_disabled(self):
        got = build_story(LOSS, ai=FakeAI(LOSS_PLAN), allow_solemn=False)
        self.assertFalse(got["ok"])

    def test_loss_with_party_design_is_refused(self):
        party = {**LOSS_PLAN, "kind": "movie"}
        got = build_story(LOSS, ai=FakeAI(party))
        self.assertFalse(got["ok"])
        self.assertTrue(got["reason"].startswith("tone_match"))

    def test_hoax_never_reaches_ai(self):
        hoax = item("Ramesh Kumar", [("A", "Ramesh Kumar death hoax"), ("B", "Ramesh Kumar dies? fake news")])
        got = build_story(hoax, ai=FakeAI(LOSS_PLAN))
        self.assertFalse(got["ok"])
        self.assertIsNone(got["plan"])

    def test_ai_says_sensitive_means_no_story(self):
        got = build_story(CONGRATS, ai=FakeAI({**CONGRATS_PLAN, "sensitive": True}))
        self.assertFalse(got["ok"])

    def test_ai_down_uses_safe_fallback_only(self):
        class Down:
            ready = True

            def chat(self, *a, **k):
                raise RuntimeError("down")
        sports = item("IPL final tonight", [("A", "IPL final tonight preview"), ("B", "IPL final tonight match")])
        self.assertTrue(build_story(sports, ai=Down())["ok"])
        self.assertFalse(build_story(LOSS, ai=Down())["ok"])


if __name__ == "__main__":
    unittest.main()
