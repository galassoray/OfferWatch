import copy
import json
import unittest
from pathlib import Path

import compare
from owcore import compare_key, normalize, safe_href

HERE = Path(__file__).resolve().parent


def load(name):
    return json.loads((HERE / name).read_text())


def page(pid='P', at='2026-10-06T16:00:00Z', facts=None, **extra):
    record = {'tenant_id': 't-one', 'id': pid, 'url': 'https://example.com/p', 'observed_at': at, 'status': 'ok',
              'facts': {'advertised_offer': '$79 tune-up'} if facts is None else facts,
              'reviewed': True, 'evidence_note': 'synthetic'}
    record.update(extra)
    return record


def snap(at, *pages, **extra):
    return dict({'tenant_id': 't-one', 'as_of': at, 'synthetic': True, 'pages': list(pages)}, **extra)


B0 = snap('2026-10-01T16:00:00Z', page(at='2026-10-01T16:00:00Z'))


def event(before, after, pid='P'):
    return next(e for e in compare.compare(before, after)['events'] if e['page_id'] == pid)


class FixtureTests(unittest.TestCase):
    def test_demo_report_matches_checked_in_html(self):
        report = compare.compare(load('before.json'), load('after.json'))
        self.assertEqual(compare.render(report), (HERE / 'comparison_demo.html').read_text())

    def test_statuses(self):
        events = {e['page_id']: e for e in compare.compare(load('before.json'), load('after.json'))['events']}
        self.assertEqual(events['Fictional North HVAC']['status'], 'CHANGE TO REVIEW')
        self.assertEqual(events['Fictional East Heating']['status'], 'NO CHANGE IN TRACKED FIELDS')
        self.assertEqual((events['Fictional South Air']['status'], events['Fictional South Air']['details']),
                         ('CHECK FAILED', []))
        self.assertEqual(events['Fictional West Comfort']['status'], 'BASELINE ONLY')


class TenantTests(unittest.TestCase):
    def test_missing_or_invalid_tenant_rejected(self):
        for bad in (None, '', 'Agency A', 'a', '../x'):
            s = snap('2026-10-06T16:00:00Z', page())
            s['tenant_id'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                compare.validate(s)

    def test_cross_tenant_comparison_rejected(self):
        after = snap('2026-10-06T16:00:00Z', dict(page(), tenant_id='t-two'), tenant_id='t-two')
        with self.assertRaisesRegex(ValueError, 'different tenants'):
            compare.compare(B0, after)

    def test_page_tenant_must_match_snapshot(self):
        with self.assertRaisesRegex(ValueError, 'tenant_id does not match'):
            compare.validate(snap('2026-10-06T16:00:00Z', dict(page(), tenant_id='t-two')))

    def test_event_ids_differ_per_tenant_and_ignore_notes(self):
        after = snap('2026-10-06T16:00:00Z', page(facts={'advertised_offer': '$59'}))
        noted = copy.deepcopy(after)
        noted['pages'][0]['evidence_note'] = 'note edited'
        self.assertEqual(event(B0, after)['event_id'], event(B0, noted)['event_id'])

        def retag(s, t):
            s = copy.deepcopy(s)
            s['tenant_id'] = t
            for p in s['pages']:
                p['tenant_id'] = t
            return s
        self.assertNotEqual(event(B0, after)['event_id'], event(retag(B0, 't-two'), retag(after, 't-two'))['event_id'])


class ReliabilityTests(unittest.TestCase):
    def test_presentation_differences_are_not_changes(self):
        for value in ('$79 tune-up ', '$79 tune-up', '＄79 tune-up', '$79​ tune-up', '$79 tune‑up'):
            with self.subTest(value=value):
                self.assertEqual(event(B0, snap('2026-10-06T16:00:00Z', page(facts={'advertised_offer': value})))['status'],
                                 'NO CHANGE IN TRACKED FIELDS')

    def test_meaningful_differences_are_changes(self):
        for value in ('$97 tune-up', '$79 tune-up*', '$79 tune-up²', '$79 tune-up until 10/31', '$79.00 tune-up',
                      '$79 Tune-Up'):
            with self.subTest(value=value):
                self.assertEqual(event(B0, snap('2026-10-06T16:00:00Z', page(facts={'advertised_offer': value})))['status'],
                                 'CHANGE TO REVIEW')

    def test_case_is_field_specific(self):
        rules = {'financing': {'case': 'insensitive'}}
        before = snap('2026-10-01T16:00:00Z', page(at='2026-10-01T16:00:00Z', facts={'financing': '0% APR', 'advertised_offer': 'A'}))
        after = snap('2026-10-06T16:00:00Z', page(facts={'financing': '0% apr', 'advertised_offer': 'a'}), field_rules=rules)
        details = event(before, after)['details']
        self.assertEqual([d['field'] for d in details], ['advertised_offer'])
        self.assertEqual(compare_key('financing', '0% APR', rules), compare_key('financing', '0% apr', rules))

    def test_incomplete_capture_is_not_a_change_or_removal(self):
        after = snap('2026-10-06T16:00:00Z', page(facts={}, missing_fields=['advertised_offer']))
        e = event(B0, after)
        self.assertEqual(e['status'], 'INCOMPLETE CAPTURE')
        self.assertEqual(e['details'][0]['kind'], 'not_extracted')
        html = compare.render(compare.compare(B0, after))
        self.assertIn('Not extracted — not evidence the offer ended', html)

    def test_empty_values_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Empty fact values'):
            compare.validate(snap('2026-10-06T16:00:00Z', page(facts={'advertised_offer': '  '})))

    def test_reviewer_confirmed_absence(self):
        after = snap('2026-10-06T16:00:00Z', page(facts={}, confirmed_absent=['advertised_offer'], review_method='interactive_cli'))
        e = event(B0, after)
        self.assertEqual((e['status'], e['details'][0]['kind']), ('CHANGE TO REVIEW', 'reviewer_confirmed_absent'))
        with self.assertRaisesRegex(ValueError, 'human review'):
            compare.validate(snap('2026-10-06T16:00:00Z', page(facts={}, confirmed_absent=['advertised_offer'])))

    def test_old_baseline_is_flagged_with_age(self):
        old = snap('2026-10-01T16:00:00Z', page(at='2025-01-01T00:00:00Z', facts={'advertised_offer': '$99'}))
        e = event(old, snap('2026-10-06T16:00:00Z', page()))
        self.assertIn('old_baseline', e['flags'])
        self.assertIn('days older', e['reason'])

    def test_pending_and_expected_pages_always_shown_with_age(self):
        after = snap('2026-10-06T16:00:00Z', page(at='2026-10-03T16:00:00Z'), pending_review=['P'], expected_pages=['P', 'Q'])
        events = {e['page_id']: e for e in compare.compare(B0, after)['events']}
        self.assertEqual(events['P']['status'], 'PENDING REVIEW')
        self.assertEqual(events['P']['age_hours'], 72.0)
        self.assertEqual(events['Q']['status'], 'NOT CHECKED')
        html = compare.render(compare.compare(B0, after))
        self.assertIn('3.0 days before report', html)
        self.assertIn('Pending review: 1', html)

    def test_synthetic_must_be_explicit_and_not_mixed(self):
        after = snap('2026-10-06T16:00:00Z', page(), synthetic=False)
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            compare.compare(B0, after)
        missing = snap('2026-10-06T16:00:00Z', page())
        del missing['synthetic']
        with self.assertRaises(ValueError):
            compare.validate(missing)

    def test_malformed_input_raises_value_error(self):
        broken = page()
        del broken['facts']
        with self.assertRaisesRegex(ValueError, 'missing'):
            compare.validate(snap('2026-10-06T16:00:00Z', broken))
        with self.assertRaises(ValueError):
            compare.validate(snap('2026-10-06T16:00:00Z', page(), dict(page(), id=7)))

    def test_chronology_timezones_and_review(self):
        with self.assertRaises(ValueError):
            compare.compare(snap('2026-10-06T16:00:00Z', page()), B0)
        with self.assertRaises(ValueError):
            compare.validate(snap('2026-10-01T16:00:00', page(at='2026-10-01T16:00:00Z')))
        with self.assertRaises(ValueError):
            compare.validate(snap('2026-10-06T16:00:00Z', page(reviewed=False)))
        with self.assertRaisesRegex(ValueError, 'attributed human review'):
            compare.validate(snap('2026-10-06T16:00:00Z', page(capture_method='automated')))


class RenderSafetyTests(unittest.TestCase):
    def test_render_escapes_source_text(self):
        after = snap('2026-10-06T16:00:00Z', page(pid='<img src=x onerror=1>', facts={'<b>f</b>': '</td><script>x()</script>'}))
        html = compare.render(compare.compare(snap('2026-10-01T16:00:00Z'), after))
        for tag in ('<script', '<img', '<b>'):
            self.assertNotIn(tag, html)

    def test_render_withholds_unsafe_urls_it_did_not_validate(self):
        report = {'tenant_id': 't-one', 'as_of': 'x', 'synthetic': False, 'events': [
            {'tenant_id': 't-one', 'status': 's', 'page_id': 'p', 'details': [], 'observed_at': None, 'age_hours': None,
             'evidence_note': '', 'url': url} for url in ('javascript:alert(1)', 'https://u:p@x.example/', 'https://x.example/"onmouseover=1')]}
        html = compare.render(report)
        self.assertNotIn('href=', html)
        self.assertEqual(html.count('Source link withheld'), 3)
        self.assertIsNone(safe_href('data:text/html,x'))

    def test_render_rejects_mixed_tenant_events(self):
        with self.assertRaises(ValueError):
            compare.render({'tenant_id': 't-one', 'synthetic': True, 'as_of': 'x',
                            'events': [{'tenant_id': 't-two', 'status': 's', 'page_id': 'p', 'details': []}]})

    def test_output_paths_cannot_collide(self):
        with self.assertRaisesRegex(ValueError, '.html'):
            compare.output_paths('out.json', ['a.json', 'b.json'])
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            compare.output_paths('before.html', ['before.json', 'after.json'])
        html_path, json_path = compare.output_paths('report.html', ['a.json', 'b.json'])
        self.assertEqual(json_path.name, 'report.json')

    def test_normalize_preserves_raw_meaning(self):
        self.assertEqual(normalize('  0% APR for 12 months '), '0% APR for 12 months')
        self.assertEqual(normalize('½ off — today'), '½ off — today')


if __name__ == '__main__':
    unittest.main()
