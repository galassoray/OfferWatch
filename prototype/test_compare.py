import json
import unittest
from pathlib import Path

import compare

HERE = Path(__file__).resolve().parent


def load(name):
    return json.loads((HERE / name).read_text())


class CompareTests(unittest.TestCase):
    def test_demo_report_matches_checked_in_html(self):
        report = compare.compare(load('before.json'), load('after.json'))
        self.assertEqual(compare.render(report), (HERE / 'comparison_demo.html').read_text())

    def test_statuses(self):
        events = {e['page_id']: e for e in compare.compare(load('before.json'), load('after.json'))['events']}
        self.assertEqual(events['Fictional North HVAC']['status'], 'CHANGE TO REVIEW')
        self.assertEqual(events['Fictional East Heating']['status'], 'NO CHANGE IN TRACKED FIELDS')
        self.assertEqual(events['Fictional South Air']['status'], 'CHECK FAILED')
        self.assertEqual(events['Fictional South Air']['details'], [])  # failed check never implies removal
        self.assertEqual(events['Fictional West Comfort']['status'], 'BASELINE ONLY')

    def test_unreviewed_success_rejected(self):
        after = load('after.json')
        after['pages'][0]['reviewed'] = False
        with self.assertRaises(ValueError):
            compare.validate(after)

    def test_automated_capture_needs_attributed_review(self):
        after = load('after.json')
        after['pages'][0]['capture_method'] = 'automated'
        with self.assertRaisesRegex(ValueError, 'attributed human review'):
            compare.validate(after)
        after['pages'][0].update(review_method='interactive_cli', reviewed_by='R', reviewed_at='2026-10-06T17:00:00Z')
        compare.validate(after)

    def test_chronology_and_timezones(self):
        with self.assertRaises(ValueError):
            compare.compare(load('after.json'), load('before.json'))
        before = load('before.json')
        before['as_of'] = '2026-10-01T16:00:00'
        with self.assertRaises(ValueError):
            compare.validate(before)

    def test_render_escapes_source_text(self):
        after = load('after.json')
        after['pages'][0]['facts']['advertised_offer'] = '</td><script>alert(1)</script>'
        after['pages'][0]['id'] = '<img src=x onerror=1>'
        html = compare.render(compare.compare(load('before.json'), after))
        self.assertNotIn('<script', html)
        self.assertNotIn('<img', html)


if __name__ == '__main__':
    unittest.main()
