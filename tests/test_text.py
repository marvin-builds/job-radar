import unittest

from job_radar.text import markdown_to_html


class MarkdownToHtmlTest(unittest.TestCase):
    def test_headings_become_bold_lines(self):
        self.assertEqual(markdown_to_html("# Profil\n## Ziele"), "<b>Profil</b>\n<b>Ziele</b>")

    def test_bold_and_italic(self):
        self.assertEqual(markdown_to_html("**fest** und *gern* und _auch_ und __so__"),
                         "<b>fest</b> und <i>gern</i> und <i>auch</i> und <b>so</b>")

    def test_snake_case_and_lone_stars_stay_text(self):
        self.assertEqual(markdown_to_html("max_per_run und 3 * 4 = 12"), "max_per_run und 3 * 4 = 12")

    def test_escapes_html(self):
        self.assertEqual(markdown_to_html("Gehalt < 50k & mehr <b>"), "Gehalt &lt; 50k &amp; mehr &lt;b&gt;")

    def test_bullets_with_nested_bold(self):
        self.assertEqual(markdown_to_html("- **Muss:** Teilzeit\n* Garten\n  - Gemüse"),
                         "• <b>Muss:</b> Teilzeit\n• Garten\n  ◦ Gemüse")

    def test_links_only_for_http(self):
        self.assertEqual(markdown_to_html("[Seite](https://x.de/?a=1&b=2) [böse](javascript:alert(1))"),
                         '<a href="https://x.de/?a=1&amp;b=2">Seite</a> böse')

    def test_table_becomes_lines(self):
        md = "| Ort | Stunden | Gehalt |\n|---|---|---|\n| Kassel | 20 | 3000 |"
        self.assertEqual(markdown_to_html(md), "• Ort: Kassel, Stunden: 20, Gehalt: 3000")

    def test_two_column_table_becomes_key_value_lines(self):
        md = "| Was | Wunsch |\n|---|---|\n| Stunden | 20-30 |\n| Schicht | nein |"
        self.assertEqual(markdown_to_html(md), "• Stunden: 20-30\n• Schicht: nein")

    def test_rules_and_extra_blank_lines_go(self):
        self.assertEqual(markdown_to_html("a\n\n\n---\n\nb"), "a\n\nb")

    def test_tags_never_span_lines(self):
        out = markdown_to_html("**halb\nfett**")
        for line in out.splitlines():
            self.assertEqual(line.count("<b>"), line.count("</b>"))


if __name__ == "__main__":
    unittest.main()
