import unittest

from publishing.server import _render_page


class ServerSecurityTests(unittest.TestCase):
    def test_dynamic_oauth_page_values_are_html_escaped(self):
        page = _render_page("<script>alert(1)</script>", '<img src=x onerror="alert(1)">')
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;img", page)

    def test_page_keeps_expected_safe_structure(self):
        page = _render_page("Instagram connected", "@example is linked.")
        self.assertIn("<h2>Instagram connected</h2>", page)
        self.assertIn("<p>@example is linked.</p>", page)


if __name__ == "__main__":
    unittest.main()
