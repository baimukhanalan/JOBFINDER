from pathlib import Path
import unittest
from dataclasses import replace
from qa_bot.domain.observation import BrowserSnapshot
from qa_bot.perception.extractor import QuestionExtractor, CATALOG, parse_timer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "questions"


class ExtractorTests(unittest.TestCase):
    def html(self, name="ANA-01"):
        return (FIXTURES / (name + ".html")).read_text(encoding="utf-8")

    def extract(self, html):
        return QuestionExtractor().extract(BrowserSnapshot(
            "obs", "http://127.0.0.1:8000/question.html", question_html=html))

    def test_all_catalog_types(self):
        for name, (section, kind) in CATALOG.items():
            with self.subTest(type=name):
                result = self.extract(self.html(name))
                self.assertTrue(result.ready, result.errors)
                self.assertEqual(result.spec.response_contract.kind, kind)
                self.assertEqual(result.spec.section, section)
                self.assertEqual(result.spec.question_number, 1)
                self.assertEqual(result.spec.remaining_seconds, 330)
                self.assertFalse(result.spec.navigation[1].enabled)

    def test_unknown_blocks(self):
        self.assertFalse(self.extract(self.html("unknown")).ready)

    def test_negative_fields(self):
        base = self.html()
        cases = [
            base.replace('data-type-id="ANA-01"', 'data-type-id="NEW-01"'),
            base.replace('data-option-count="4"', 'data-option-count="5"'),
            base.replace('data-question-id="q-ANA-01"', 'data-question-id=""'),
            base.replace('data-field="instruction"', 'data-field="removed"'),
            base.replace('data-option-id="o2"', 'data-option-id="o1"'),
            base.replace('05:30', '00:00'),
            base.replace('05:30', '05:99'),
            base.replace('</main>', '<canvas></canvas></main>'),
            base.replace('data-kind="single_choice"', 'data-kind="text"'),
            base.replace('</main>', '<p data-field="text">Duplicate</p></main>'),
        ]
        for html in cases:
            with self.subTest(html=html[:30]):
                result = self.extract(html)
                self.assertFalse(result.ready)
                self.assertIsNone(result.spec)
                self.assertTrue(result.errors)

    def test_missing_timer_lower_confidence(self):
        result = self.extract(self.html().replace('<span data-field="timer">05:30</span>', ''))
        self.assertTrue(result.ready)
        self.assertIsNone(result.spec.remaining_seconds)
        self.assertLess(result.confidence, 1)
        self.assertIn("timer_unavailable", result.warnings)

    def test_hash_excludes_clock_and_instance(self):
        first = self.extract(self.html()).spec
        second = self.extract(self.html().replace("05:30", "05:29")
                              .replace("q-ANA-01", "q-another")).spec
        self.assertEqual(first.content_hash, second.content_hash)
        changed = self.extract(self.html().replace("Choice 1", "Changed")).spec
        self.assertNotEqual(first.content_hash, changed.content_hash)

    def test_tables_constraints_media(self):
        table = self.extract(self.html("ANA-02")).spec.tables[0]
        self.assertEqual(table.rows, (("A", "12"),))
        text = self.extract(self.html("WRITEX-01")).spec
        self.assertEqual(text.response_contract.min_words, 30)
        self.assertIn("maxlength", [c.name for c in text.field_constraints])
        image = self.extract(self.html("ANA-03")).spec.assets[0]
        self.assertEqual(image.location, "http://127.0.0.1:8000/diagram.svg")
        audio = self.extract(self.html("SVAR-01")).spec.assets[0]
        self.assertEqual(audio.media_type, "audio/wav")

    def test_timer_formats(self):
        self.assertEqual(parse_timer("1:02:03"), 3723)
        self.assertEqual(parse_timer("40"), 40)
        for bad in ("soon", "-1", "1:99", "1:02:99"):
            with self.assertRaises(ValueError):
                parse_timer(bad)

    def test_missing_required_table_and_image(self):
        table_html = self.html("ANA-02").replace("<table>", "<div>").replace("</table>", "</div>")
        image_html = self.html("ANA-03").replace("<img ", "<input ")
        for html in (table_html, image_html):
            self.assertFalse(self.extract(html).ready)

    def test_merged_table_rejected(self):
        self.assertFalse(self.extract(self.html("ANA-02")
                                     .replace("<td>A", '<td colspan="2">A')).ready)

    def test_hidden_and_image_option(self):
        html = self.html().replace('Choice 1</label>',
            '<img id="choice-image" src="diagram.svg" alt=""></label>')
        html = html.replace('</main>',
            '<div hidden data-option-id="hidden-choice">Do not collect</div></main>')
        result = self.extract(html)
        self.assertTrue(result.ready, result.errors)
        self.assertEqual(len(result.spec.options), 4)
        self.assertEqual(result.spec.options[0].image_ref, "choice-image")

    def test_duplicate_media_and_nav(self):
        for html in (
            self.html("ANA-03").replace('</main>', '<img id="diagram" src="other.svg"></main>'),
            self.html().replace('id="back"', 'id="submit"'),
        ):
            self.assertFalse(self.extract(html).ready)
