import unittest

import copywriter as cw


class HashtagTests(unittest.TestCase):
    def test_caps_at_five_even_if_model_returns_many(self):
        tags = [f"#tag{i}abc" for i in range(12)]
        self.assertEqual(len(cw.sanitize_hashtags(tags)), 5)

    def test_cleans_dedupes_and_lowercases(self):
        out = cw.sanitize_hashtags(["#Street Style", "streetstyle", "#STREETSTYLE!", "# cafe-corner"])
        self.assertEqual(out, ["#streetstyle", "#cafecorner"])

    def test_drops_generic_spam_and_numbers(self):
        out = cw.sanitize_hashtags(["#love", "#instagood", "#fyp", "#123456", "#ab", "#goldenhour"])
        self.assertEqual(out, ["#goldenhour"])

    def test_keep_generic_when_asked(self):
        self.assertEqual(cw.sanitize_hashtags(["#love"], drop_generic=False), ["#love"])

    def test_accepts_plain_string(self):
        self.assertEqual(cw.sanitize_hashtags("#aaa, #bbb #ccc"), ["#aaa", "#bbb", "#ccc"])

    def test_non_list_input_is_safe(self):
        self.assertEqual(cw.sanitize_hashtags(None), [])
        self.assertEqual(cw.sanitize_hashtags(42), [])

    def test_tags_repeating_caption_words_move_last(self):
        out = cw.sanitize_hashtags(["#sunset", "#beachwalk", "#filmlook"], avoid_words="Sunset on the sand")
        self.assertEqual(out, ["#beachwalk", "#filmlook", "#sunset"])


class CaptionTests(unittest.TestCase):
    def test_removes_hashtags_and_quotes(self):
        self.assertEqual(cw.clean_caption('"golden light, quiet street #vibes #love"'),
                         "golden light, quiet street")

    def test_trims_long_text_at_sentence(self):
        text = ("First sentence here. " * 30).strip()
        out = cw.clean_caption(text, max_chars=100)
        self.assertLessEqual(len(out), 100)
        self.assertTrue(out.endswith("."))

    def test_keeps_line_breaks(self):
        self.assertEqual(cw.clean_caption("line one\n\n\n\nline two"), "line one\n\nline two")

    def test_parse_copy_rejects_empty_caption(self):
        with self.assertRaises(ValueError):
            cw.parse_copy({"caption": "  #only #tags ", "hashtags": []}, mode="soft")

    def test_parse_copy_shape(self):
        out = cw.parse_copy({"caption": "Tea on the balcony", "hashtags": ["#teatime", "#love"]}, mode="soft")
        self.assertEqual(out, {"caption": "Tea on the balcony", "hashtags": ["#teatime"],
                               "caption_style": "soft"})


class PromptTests(unittest.TestCase):
    def test_prompt_enforces_five_tags_and_hook(self):
        p = cw.build_prompt(mode="baddie", vibe="baddie")
        self.assertIn("MAX 5", p)
        self.assertIn("90 characters", p)

    def test_hinglish_and_recent_captions_reach_prompt(self):
        p = cw.build_prompt(mode="funny", vibe="soft", language="hinglish",
                            recent_captions=["same old caption"])
        self.assertIn("Hinglish", p)
        self.assertIn("same old caption", p)

    def test_unknown_mode_falls_back(self):
        self.assertIn("authentic", cw.build_prompt(mode="zzz", vibe="zzz"))


class PublishLimitTests(unittest.TestCase):
    def test_build_caption_never_publishes_more_than_five_tags(self):
        from publishing import service
        text = service.build_caption({"caption": "hi", "hashtags": [f"#t{i}xx" for i in range(10)]})
        self.assertEqual(text.count("#"), 5)


if __name__ == "__main__":
    unittest.main()
