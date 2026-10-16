import io
import unittest

import numpy as np
from PIL import Image

import editor


def _jpeg(arr):
    b = io.BytesIO()
    Image.fromarray(arr).save(b, "JPEG", quality=95)
    return b.getvalue()


def _scene(w=640, h=480, subject_x=None):
    """Gradient background, optional skin-coloured blob = 'subject'."""
    y, x = np.mgrid[0:h, 0:w]
    base = np.stack([100 + 40 * x / w, 120 + 30 * y / h, 150 + 0 * x], -1).astype(np.uint8)
    if subject_x is not None:
        cx, cy, r = subject_x, h // 2, h // 5
        m = (x - cx) ** 2 + (y - cy) ** 2 < r * r
        base[m] = (225, 170, 140)
    return base


def _median(buf):
    return float(np.median(np.asarray(Image.open(buf).convert("L")))) / 255


class EditorTests(unittest.TestCase):
    def test_every_style_and_size(self):
        src = _jpeg(_scene())
        for style in editor.STYLES:
            for size, dim in editor.SIZES.items():
                self.assertEqual(Image.open(editor.edit_photo(src, style, size)).size, dim)

    def test_unknown_style_falls_back(self):
        out, rep = editor.edit_photo(_jpeg(_scene()), "nope", "post", return_report=True)
        self.assertEqual(rep["style"], "natural")
        self.assertEqual(Image.open(out).size, (1080, 1350))

    def test_original_size_keeps_dimensions(self):
        self.assertEqual(Image.open(editor.edit_photo(_jpeg(_scene(500, 300)), "soft", "original")).size, (500, 300))

    def test_dark_photo_is_lifted(self):
        dark = (_scene().astype(np.float32) * 0.55).astype(np.uint8)   # about -2 stops
        before = float(np.median(dark.mean(-1))) / 255
        out, rep = editor.edit_photo(_jpeg(dark), "natural", "post", return_report=True)
        self.assertGreater(_median(out), before + 0.08)
        self.assertGreater(rep["exposure_stops"], 0.3)

    def test_moody_keeps_it_darker_than_natural(self):
        dark = (_scene().astype(np.float32) * 0.55).astype(np.uint8)
        moody = _median(editor.edit_photo(_jpeg(dark), "moody", "post"))
        natural = _median(editor.edit_photo(_jpeg(dark), "natural", "post"))
        self.assertLess(moody, natural)

    def test_well_exposed_photo_is_barely_touched_by_auto_exposure(self):
        _, rep = editor.edit_photo(_jpeg(_scene()), "natural", "post", return_report=True)
        self.assertLess(abs(rep["exposure_stops"]), 0.5)

    def test_bright_photo_is_not_blown_out(self):
        bright = np.clip(_scene().astype(np.float32) * 1.7, 0, 255).astype(np.uint8)
        out = editor.edit_photo(_jpeg(bright), "natural", "post")
        self.assertLess(_median(out), float(np.median(bright.mean(-1))) / 255)

    def test_noise_is_reduced(self):
        rng = np.random.default_rng(1)
        noisy = np.clip(_scene().astype(np.float32) + rng.normal(0, 22, (480, 640, 3)), 0, 255).astype(np.uint8)
        img = editor._load(_jpeg(noisy))
        before = editor._estimate_noise(img)
        after = editor._estimate_noise(editor._denoise(img)[0])
        self.assertGreater(before, 0.03)
        self.assertLess(after, before * 0.7)

    def test_clean_photo_is_not_denoised(self):
        img = editor._load(_jpeg(_scene()))
        self.assertIs(editor._denoise(img)[0], img)

    def test_vibrance_protects_skin_more_than_dull_blue(self):
        a = np.zeros((1, 2, 3), np.float32)
        a[0, 0] = [0.80, 0.60, 0.50]      # skin
        a[0, 1] = [0.45, 0.50, 0.62]      # dull blue
        b = editor._saturate(a.copy(), 1.4)

        def sat(p):
            return (p.max() - p.min()) / p.max()
        self.assertLess(sat(b[0, 0]) - sat(a[0, 0]), sat(b[0, 1]) - sat(a[0, 1]))

    def test_crop_follows_subject_without_opencv(self):
        saved, editor.cv2 = editor.cv2, None
        try:
            img = editor._load(_jpeg(_scene(900, 400, subject_x=760)))
            focus = editor._find_focus(img)
            self.assertEqual(focus[4], "saliency")
            box = editor._crop_box(*img.size, (1080, 1350), focus)
            self.assertGreater(box[0], 300)                       # moved right
            self.assertAlmostEqual((box[2] - box[0]) / (box[3] - box[1]), 1080 / 1350, places=2)
        finally:
            editor.cv2 = saved

    def test_crop_box_stays_inside_image_and_keeps_head(self):
        # tall image -> story crop trims top/bottom; face near top must stay inside
        focus = (0.5, 0.15, 0.2, 0.12, "face")
        l, t, r, b = editor._crop_box(1000, 1600, (1080, 1350), focus)
        self.assertGreaterEqual(t, 0)
        self.assertLessEqual(b, 1600)
        self.assertLessEqual(t, (0.15 - 0.06) * 1600)             # head top not cut

    def test_no_crop_when_aspect_matches(self):
        self.assertEqual(editor._crop_box(1080, 1350, (1080, 1350), (0.5, 0.5, 0, 0, "saliency")),
                         (0.0, 0.0, 1080.0, 1350.0))

    def test_grayscale_and_rgba_inputs(self):
        gray = Image.fromarray(_scene()[..., 0])
        b = io.BytesIO(); gray.save(b, "PNG")
        self.assertEqual(Image.open(editor.edit_photo(b.getvalue(), "film", "post")).size, (1080, 1350))
        rgba = Image.fromarray(_scene()).convert("RGBA")
        b = io.BytesIO(); rgba.save(b, "PNG")
        self.assertEqual(Image.open(editor.edit_photo(b.getvalue(), "clean", "story")).size, (1080, 1920))

    def test_text_overlay_still_works(self):
        a = editor.edit_photo(_jpeg(_scene()), "baddie", "post")
        b = editor.edit_photo(_jpeg(_scene()), "baddie", "post", text="hello world", text_style="bold")
        self.assertNotEqual(a.getvalue(), b.getvalue())


if __name__ == "__main__":
    unittest.main()
