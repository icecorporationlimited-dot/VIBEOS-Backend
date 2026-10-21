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


if __name__ == "__main__":
    unittest.main()
