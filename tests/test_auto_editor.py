"""Regression tests for VIBEOS fully automatic photo editing decisions."""
from __future__ import annotations

import io
import sys
import unittest
from unittest.mock import patch

from PIL import Image

# Test files run from the backend root in CI/worker images.
import job_handlers


def image_bytes(size, color=(110, 120, 130)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


class AutomaticEditorTests(unittest.TestCase):
    def test_confident_tagger_style_is_normalized_to_editor_style(self):
        fake = {
            "style": "baddie",
            "style_label": "Y2K / Digicam Flash",
            "style_confidence": 82,
            "low_confidence": False,
            "edit": {"exposure": 0.2},
        }
        with patch("tagger_client.tag_photo", return_value=fake):
            style, details = job_handlers._choose_automatic_style(image_bytes((400, 600)))
        self.assertEqual(style, "y2k")
        self.assertEqual(details["style_source"], "tagger")
        self.assertEqual(details["style_id"], "y2k")

    def test_low_confidence_uses_builtin_auto_fallback(self):
        fake = {"style": "mono", "style_confidence": 30, "low_confidence": True}
        with patch("tagger_client.tag_photo", return_value=fake):
            style, details = job_handlers._choose_automatic_style(image_bytes((400, 600)))
        self.assertEqual(style, "natural")
        self.assertEqual(details["style_id"], "natural")

    def test_output_size_is_chosen_from_source_aspect_ratio(self):
        self.assertEqual(job_handlers._choose_output_size(image_bytes((500, 1000))), "story")
        self.assertEqual(job_handlers._choose_output_size(image_bytes((800, 800))), "square")
        self.assertEqual(job_handlers._choose_output_size(image_bytes((1200, 1600))), "post")


# ---------------------------------------------------------------- editor quality fixes
import importlib.util
from pathlib import Path

import numpy as np

import editor


def _jpeg(arr):
    out = io.BytesIO()
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(out, "JPEG", quality=95)
    return out.getvalue()


def _scene(w, h, bg=(90, 110, 140), patches=(), noise=0.0, seed=3):
    a = np.zeros((h, w, 3), np.float32) + np.array(bg, np.float32)
    for (x0, y0, x1, y1, col) in patches:
        a[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = col
    if noise:
        a += np.random.default_rng(seed).normal(0, noise, a.shape)
    return a


class SkinMaskTests(unittest.TestCase):
    def test_detects_light_medium_and_deep_skin_but_not_grey_sky_or_grass(self):
        skins = [(217, 166, 140), (190, 140, 105), (89, 56, 43), (60, 38, 30)]
        others = [(128, 128, 128), (110, 150, 210), (60, 140, 60), (20, 20, 20)]
        arr = np.array([skins + others], dtype=np.float32) / 255.0
        mask = editor._skin_mask(arr)[0]
        self.assertTrue(all(mask[:4] == 1), mask)
        self.assertTrue(all(mask[4:] == 0), mask)


class ExposureTests(unittest.TestCase):
    def test_dark_moody_photo_is_not_lifted_to_daylight(self):
        arr = _scene(1200, 900, bg=(26, 26, 34), patches=[(0.1, 0.1, 0.14, 0.14, (255, 240, 200))], noise=2)
        _out, rep = editor.edit_photo(_jpeg(arr), "moody", "post", return_report=True)
        self.assertLessEqual(rep["exposure_stops"], editor.EXPO_LIMITS["moody"][1] + 1e-6)

    def test_bright_highlights_limit_the_push(self):
        # mostly dark, but 5 % of the frame is already near-white: do not blow it out
        arr = _scene(1200, 900, bg=(40, 40, 46), patches=[(0.0, 0.0, 0.3, 0.2, (252, 252, 252))])
        _out, rep = editor.edit_photo(_jpeg(arr), "clean", "post", return_report=True)
        self.assertLess(rep["exposure_stops"], 0.9)


class FramingTests(unittest.TestCase):
    FACE = (0.46, 0.25, 0.10, 0.22)       # x, y, w, h of the original frame

    def test_wide_photo_with_known_face_is_cropped_not_padded_and_keeps_face(self):
        arr = _scene(2000, 1125, patches=[(0.46, 0.25, 0.56, 0.47, (200, 150, 125))])
        _out, rep = editor.edit_photo(_jpeg(arr), "clean", "post", return_report=True,
                                      faces=[list(self.FACE)])
        self.assertEqual(rep["crop"], "face")

    def test_wide_photo_with_one_skin_blob_and_no_faces_is_cropped_via_skin_box(self):
        arr = _scene(2000, 1125, patches=[(0.44, 0.25, 0.58, 0.60, (200, 150, 125))])
        _out, rep = editor.edit_photo(_jpeg(arr), "clean", "post", return_report=True)
        self.assertEqual(rep["crop"], "skinbox")

    def test_people_at_both_edges_without_faces_falls_back_to_pad(self):
        arr = _scene(2000, 1125, patches=[(0.02, 0.3, 0.10, 0.7, (200, 150, 125)),
                                          (0.88, 0.3, 0.98, 0.7, (190, 140, 110))])
        _out, rep = editor.edit_photo(_jpeg(arr), "clean", "post", return_report=True)
        self.assertEqual(rep["crop"], "pad")

    def test_scenery_without_people_gets_saliency_crop(self):
        arr = _scene(2000, 1125, bg=(70, 120, 170))
        _out, rep = editor.edit_photo(_jpeg(arr), "clean", "post", return_report=True)
        self.assertEqual(rep["crop"], "saliency")

    def test_output_sizes_unchanged(self):
        arr = _scene(2000, 1125)
        self.assertEqual(Image.open(editor.edit_photo(_jpeg(arr), "clean", "post")).size, (1080, 1350))
        self.assertEqual(Image.open(editor.edit_photo(_jpeg(arr), "clean", "story")).size, (1080, 1920))


class SkinRetouchTests(unittest.TestCase):
    def _face_image(self, noise):
        # a skin disc with fine texture, a dark eyebrow-like stripe, on a flat background
        h, w = 1350, 1080
        a = _scene(w, h, bg=(70, 90, 120))
        yy, xx = np.mgrid[0:h, 0:w]
        disc = ((xx - 540) / 200.0) ** 2 + ((yy - 560) / 260.0) ** 2 < 1
        a[disc] = (205, 155, 130)
        a[disc] += np.random.default_rng(5).normal(0, noise, (int(disc.sum()), 3))
        a[470:482, 440:640] = (45, 30, 28)              # strong edge to preserve
        return _jpeg(a), disc

    def test_retouch_smooths_skin_texture_on_known_faces_and_keeps_edges(self):
        data, disc = self._face_image(noise=9)
        face = [[0.34, 0.25, 0.32, 0.38]]
        with_face, rep = editor.edit_photo(data, "clean", "post", return_report=True, faces=face)
        no_face = editor.edit_photo(data, "clean", "post", faces=None)
        self.assertGreater(rep.get("skin_retouch", 0), 0)
        a = np.asarray(Image.open(with_face).convert("L"), np.float32)
        b = np.asarray(Image.open(no_face).convert("L"), np.float32)
        cheek = (slice(620, 700), slice(450, 630))
        self.assertLess(a[cheek].std(), b[cheek].std() * 0.9)       # smoother skin
        stripe = (slice(473, 479), slice(480, 600))
        self.assertLess(a[stripe].mean(), a[cheek].mean() - 60)     # edge still dark

    def test_no_faces_and_no_selfie_tag_means_no_retouch(self):
        data, _ = self._face_image(noise=9)
        _o, rep = editor.edit_photo(data, "clean", "post", return_report=True)
        self.assertNotIn("skin_retouch", rep)

    def test_selfie_scene_tag_enables_retouch_without_faces(self):
        data, _ = self._face_image(noise=9)
        _o, rep = editor.edit_photo(data, "clean", "post", return_report=True, tags=["selfie"])
        self.assertIn("skin_retouch", rep)


# ---------------------------------------------------------------- tagger content -> editor
class TaggerContentFlowTests(unittest.TestCase):
    def test_faces_and_tags_from_tagger_reach_edit_photo(self):
        fake = {"style": "mono", "style_confidence": 30, "low_confidence": True,
                "faces": [[0.3, 0.2, 0.2, 0.25], ["bad"], [5, 5, 5, 5]], "tags": ["selfie", 7]}
        with patch("tagger_client.tag_photo", return_value=fake):
            style, details = job_handlers._choose_automatic_style(image_bytes((400, 600)))
        self.assertEqual(style, "natural")
        tags, faces = job_handlers._take_detected({}, details)
        self.assertEqual(faces, [[0.3, 0.2, 0.2, 0.25]])
        self.assertEqual(tags, ["selfie"])
        self.assertNotIn("_faces", details)
        self.assertNotIn("_tags", details)

    def test_caller_supplied_tags_and_faces_win(self):
        details = {"_tags": ["selfie"], "_faces": [[0.1, 0.1, 0.2, 0.2]]}
        tags, faces = job_handlers._take_detected({"tags": ["fashion"], "faces": [[0.5, 0.5, 0.1, 0.1]]}, details)
        self.assertEqual(tags, ["fashion"])
        self.assertEqual(faces, [[0.5, 0.5, 0.1, 0.1]])

    def test_tagger_down_still_works(self):
        with patch("tagger_client.tag_photo", return_value=None):
            style, details = job_handlers._choose_automatic_style(image_bytes((400, 600)))
        self.assertEqual(style, "natural")
        self.assertEqual(job_handlers._take_detected({}, details), (None, None))


def _load_tagger_styles():
    path = Path(__file__).resolve().parent.parent / "tagger_service" / "styles.py"
    spec = importlib.util.spec_from_file_location("tagger_styles", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TaggerStyleContentTests(unittest.TestCase):
    VALUES = dict(exposure=-10, contrast=20, highlights=0, shadows=0, saturation=0, warmth=0, grain=10)
    EXTRAS = dict(sat_mean=90, p5=20, p95=200, dark_ratio=0.1, bright_ratio=0.05, noise_sigma=1.0)

    def setUp(self):
        self.st = _load_tagger_styles()

    def _night(self):
        return dict(self.VALUES, exposure=-60), dict(self.EXTRAS, dark_ratio=0.6)

    def test_night_flash_with_people_boosts_y2k_and_tags_nightout(self):
        v, e = self._night()
        content = {"n_faces": 2, "face_area": 0.04, "skin_share": 0.2, "flashy": True, "sky": 0.0}
        base = self.st.score_styles(v, e)
        adj = self.st.content_adjust(base, content, v, e)
        self.assertGreater(adj["y2k"], base["y2k"])
        self.assertIn("nightout", self.st.content_tags(content, v, e))

    def test_night_without_flash_prefers_moody_over_y2k(self):
        v, e = self._night()
        content = {"n_faces": 0, "face_area": 0, "skin_share": 0.0, "flashy": False, "sky": 0.0}
        base = self.st.score_styles(v, e)
        adj = self.st.content_adjust(base, content, v, e)
        self.assertGreater(adj["moody"], base["moody"])
        self.assertLess(adj["y2k"], base["y2k"])
        self.assertEqual(self.st.content_tags(content, v, e), ["dramatic"])

    def test_close_single_face_is_tagged_selfie_and_daylight_faces_favour_clean(self):
        content = {"n_faces": 1, "face_area": 0.2, "skin_share": 0.3, "flashy": False, "sky": 0.0}
        self.assertEqual(self.st.content_tags(content, self.VALUES, self.EXTRAS), ["selfie"])
        base = self.st.score_styles(self.VALUES, self.EXTRAS)
        adj = self.st.content_adjust(base, content, self.VALUES, self.EXTRAS)
        self.assertGreater(adj["clean"], base["clean"])

    def test_classify_is_backward_compatible_without_content(self):
        res = self.st.classify(self.VALUES, self.EXTRAS)
        self.assertIn("style", res)
        self.assertEqual(res["tags"], [])



if __name__ == "__main__":
    unittest.main()
