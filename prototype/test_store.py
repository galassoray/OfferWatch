"""Ledger tests: tenant separation, identity, idempotency, review gating, delivery state."""
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import capture
import report
import store
from owcore import iso

URL = 'https://shared.example.com/specials'
FIELDS = {'advertised_offer': {'pattern': r'Offer:\s*(.+?)\s*\|'},
          'financing': {'pattern': r'Financing:\s*(.+?)\s*\|', 'case': 'insensitive'}}


def allowlist(tenant, synthetic=True, url=URL):
    return capture.load_allowlist({
        'tenant_id': tenant, 'synthetic': synthetic, 'user_agent': 'OfferWatchBot/0.2 (+mailto:x@example.invalid)',
        'pages': [{'id': 'Shared Co', 'url': url, 'approved_by': 'contact', 'approved_at': '2026-10-01T00:00:00Z',
                   'access_rules_checked_by': 'fixture', 'access_rules_checked_at': '2026-10-01T00:00:00Z',
                   'fields': FIELDS}]})


class Clock:
    def __init__(self, start='2026-10-05T09:00:00+00:00'):
        self.now = datetime.fromisoformat(start)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def record(tenant, clock, offer='$99 tune-up', financing='0% APR 12 mo', status='ok', missing=(), anomalies=(),
           text_length=1000, url=URL):
    facts = {'advertised_offer': offer, 'financing': financing}
    facts = {k: v for k, v in facts.items() if k not in missing}
    rec = {'tenant_id': tenant, 'id': 'Shared Co', 'url': url, 'observed_at': iso(clock()), 'status': status,
           'capture_method': 'automated', 'raw_facts': dict(facts) if status == 'ok' else {},
           'facts': dict(facts) if status == 'ok' else {}, 'missing_fields': list(missing),
           'anomalies': list(anomalies), 'evidence': {}, 'source_times': {}, 'text_length': text_length,
           'content_sha256': 'h' + str(clock().timestamp()), 'evidence_note': 'n'}
    if status != 'ok':
        rec['failure_reason'] = status
        rec['status'] = 'failed'
    return rec


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.fixture = store.synthetic_session()

    def ledger(self, tenant='alpha-agency', synthetic=True, url=URL):
        led = store.Ledger(Path(self.tmp.name) / tenant / 'ledger.sqlite3', tenant, synthetic, self.clock)
        led.sync_sources(allowlist(tenant, synthetic, url), capture.access_attested)
        self.addCleanup(led.close)
        return led

    def baseline(self, led, **kw):
        obs, _ = led.ingest(record(led.tenant, self.clock, **kw))
        led.apply_review(self.fixture, obs, 'accept')
        return obs

    def capture(self, led, days=3, **kw):
        self.clock.advance(days=days)
        obs, created = led.ingest(record(led.tenant, self.clock, **kw))
        return obs, led.observation(obs)


class TenantSeparationTests(LedgerCase):
    def test_ledger_bound_to_one_tenant(self):
        self.ledger('alpha-agency').close()
        with self.assertRaisesRegex(ValueError, 'belongs to tenant'):
            store.Ledger(Path(self.tmp.name) / 'alpha-agency' / 'ledger.sqlite3', 'beta-agency', True, self.clock)
        with self.assertRaises(ValueError):
            store.Ledger(Path(self.tmp.name) / 'x.sqlite3', '', True, self.clock)

    def test_foreign_or_missing_tenant_records_rejected_before_storage(self):
        led = self.ledger('alpha-agency')
        for tenant in ('beta-agency', None):
            rec = record('alpha-agency', self.clock)
            rec['tenant_id'] = tenant
            with self.subTest(tenant=tenant), self.assertRaisesRegex(ValueError, 'tenant_id'):
                led.ingest(rec)
        self.assertEqual(led._rows('SELECT COUNT(*) n FROM observations')[0]['n'], 0)
        with self.assertRaisesRegex(ValueError, 'Allowlist tenant'):
            led.sync_sources(allowlist('beta-agency'), capture.access_attested)

    def test_unapproved_url_rejected(self):
        led = self.ledger('alpha-agency')
        with self.assertRaisesRegex(ValueError, 'approved source URL'):
            led.ingest(record('alpha-agency', self.clock, url='https://other.example.com/'))

    def test_same_url_two_agencies_stay_separate_and_cannot_suppress_each_other(self):
        alpha, beta = self.ledger('alpha-agency'), self.ledger('beta-agency')
        for led in (alpha, beta):
            self.baseline(led)
        self.clock.advance(days=3)
        for led in (alpha, beta):
            obs, _ = led.ingest(record(led.tenant, self.clock, offer='$79 tune-up'))
            self.assertEqual(led.observation(obs)['acceptance'], 'pending_review')
        alpha.apply_review(self.fixture, alpha.review_queue()[0]['obs_id'], 'accept')
        self.assertEqual(len(beta.review_queue()), 1)          # alpha's decision did not touch beta
        beta.apply_review(self.fixture, beta.review_queue()[0]['obs_id'], 'accept')
        a_ids = {t['transition_id'] for t in alpha.transitions()}
        b_ids = {t['transition_id'] for t in beta.transitions()}
        self.assertFalse(a_ids & b_ids)
        out = Path(self.tmp.name) / 'reports'
        a_report, _ = alpha.prepare_report(iso(self.clock()), out / 'a', report.render)
        alpha.release(self.fixture, a_report)
        alpha.acknowledge_delivery(self.fixture, a_report, 'manual')
        b_report, _ = beta.prepare_report(iso(self.clock()), out / 'b', report.render)
        b_events = json.loads(Path(beta.report(b_report)['json_path']).read_text())['events']
        self.assertEqual([e['kind'] for e in b_events], ['baseline', 'change'])  # alpha's delivery suppresses nothing
        self.assertTrue(all(e['transition_id'] not in a_ids for e in b_events))


class IdentityTests(LedgerCase):
    def test_reprocessing_same_observation_is_idempotent(self):
        led = self.ledger()
        rec = record(led.tenant, self.clock)
        first = led.ingest(rec)
        self.assertEqual(led.ingest(dict(rec, evidence_note='different note')), (first[0], False))
        self.assertEqual(led._rows('SELECT COUNT(*) n FROM observations')[0]['n'], 1)

    def test_unchanged_repeats_and_skipped_runs_make_no_events(self):
        led = self.ledger()
        self.baseline(led)
        for days in (3, 4, 10, 3):        # including a skipped week
            _, row = self.capture(led, days=days)
            self.assertEqual(row['acceptance'], 'auto_accepted')
        self.assertEqual([t['kind'] for t in led.transitions()], ['baseline'])

    def test_change_after_skipped_run_is_one_event(self):
        led = self.ledger()
        self.baseline(led)
        obs, row = self.capture(led, days=10, offer='$79 tune-up')
        led.apply_review(self.fixture, obs, 'accept')
        self.capture(led, days=3, offer='$79 tune-up')
        self.assertEqual([t['kind'] for t in led.transitions()], ['baseline', 'change'])

    def test_a_b_a_is_two_distinct_events(self):
        led = self.ledger()
        self.baseline(led)
        for offer in ('$79 tune-up', '$99 tune-up'):
            obs, row = self.capture(led, offer=offer)
            self.assertIn('material_change', json.loads(row['machine_reasons']))
            led.apply_review(self.fixture, obs, 'accept')
        changes = [json.loads(t['changes'])[0] for t in led.transitions() if t['kind'] == 'change']
        self.assertEqual([(c['from'], c['to']) for c in changes],
                         [('$99 tune-up', '$79 tune-up'), ('$79 tune-up', '$99 tune-up')])
        self.assertEqual(len({t['transition_id'] for t in led.transitions()}), 3)

    def test_note_edits_do_not_create_events(self):
        led = self.ledger()
        obs = self.baseline(led)
        before = [dict(t) for t in led.transitions()]
        led.set_note(obs, 'edited')
        led.set_note(obs, 'edited again')
        self.assertEqual([dict(t) for t in led.transitions()], before)

    def test_correction_retracts_and_reports_correction_after_delivery(self):
        led = self.ledger()
        self.baseline(led)
        obs, _ = self.capture(led, offer='$79 tune-up')
        led.apply_review(self.fixture, obs, 'accept')
        out = Path(self.tmp.name) / 'r'
        rid, _ = led.prepare_report(iso(self.clock()), out, report.render)
        led.release(self.fixture, rid)
        led.acknowledge_delivery(self.fixture, rid, 'manual')
        wrong = [t['transition_id'] for t in led.transitions() if t['kind'] == 'change'][0]
        self.clock.advance(hours=2)
        led.apply_review(self.fixture, obs, 'correct', {'advertised_offer': '$99 tune-up'})  # capture was misread
        self.assertEqual(led._rows('SELECT state FROM transitions WHERE transition_id=?', wrong)[0]['state'], 'retracted')
        self.assertEqual([t['kind'] for t in led.transitions(state='active')], ['baseline'])
        self.clock.advance(days=7)
        rid2, _ = led.prepare_report(iso(self.clock()), out, report.render)
        content = json.loads(Path(led.report(rid2)['json_path']).read_text())
        self.assertEqual([c['transition_id'] for c in content['corrections']], [wrong])
        self.assertEqual(content['events'], [])

    def test_correction_before_acceptance_can_cancel_a_false_change(self):
        led = self.ledger()
        self.baseline(led)
        obs, _ = self.capture(led, offer='$79 tune-up')
        led.apply_review(self.fixture, obs, 'accept', {'advertised_offer': '$99 tune-up'})
        self.assertEqual([t['kind'] for t in led.transitions()], ['baseline'])


class EligibilityTests(LedgerCase):
    def reasons(self, row):
        return json.loads(row['machine_reasons'])

    def test_first_capture_needs_human_baseline(self):
        led = self.ledger()
        obs, _ = led.ingest(record(led.tenant, self.clock))
        row = led.observation(obs)
        self.assertEqual((row['acceptance'], self.reasons(row)), ('pending_review', ['baseline_requires_human_review']))

    def test_exceptions_go_to_queue(self):
        cases = [({'missing': ('financing',)}, 'incomplete_extraction'),
                 ({'anomalies': ('ambiguous_match:advertised_offer',)}, 'extraction_anomaly:ambiguous_match:advertised_offer'),
                 ({'text_length': 300}, 'extraction_anomaly:page_size_shift'),
                 ({'offer': '$99 Tune-Up'}, 'material_change')]           # offer is case-sensitive
        for kwargs, reason in cases:
            led = self.ledger(f't-{len(reason)}-{len(kwargs)}x')
            self.baseline(led)
            _, row = self.capture(led, **kwargs)
            with self.subTest(reason=reason):
                self.assertEqual(row['acceptance'], 'pending_review')
                self.assertIn(reason, self.reasons(row))

    def test_case_insensitive_field_and_presentation_differences_auto_accept(self):
        led = self.ledger()
        self.baseline(led)
        _, row = self.capture(led, offer='$99 tune-up', financing='0% apr 12 MO')
        self.assertEqual(row['acceptance'], 'auto_accepted')
        self.assertEqual(row['accepted_rule'], store.AUTO_RULE)

    def test_stale_capture_and_periodic_reverification(self):
        led = self.ledger()
        self.baseline(led)
        self.clock.advance(days=3)
        old = record(led.tenant, self.clock)
        self.clock.advance(days=5)
        obs, _ = led.ingest(old)
        self.assertIn('stale_capture', self.reasons(led.observation(obs)))
        for _ in range(8):
            _, row = self.capture(led, days=7)
        self.assertIn('periodic_reverification_due', self.reasons(row))

    def test_confirmed_absence_is_not_reported_incomplete_again(self):
        led = self.ledger()
        self.baseline(led)
        obs, _ = self.capture(led, missing=('financing',))
        led.apply_review(self.fixture, obs, 'accept', {}, ['financing'])
        _, row = self.capture(led, missing=('financing',))
        self.assertEqual(row['acceptance'], 'auto_accepted')
        change = json.loads([t for t in led.transitions() if t['kind'] == 'change'][0]['changes'])[0]
        self.assertEqual((change['field'], change['to_kind']), ('financing', 'absent'))

    def test_failed_check_needs_no_review_and_implies_nothing(self):
        led = self.ledger()
        self.baseline(led)
        obs, row = self.capture(led, status='http_503')
        self.assertEqual((row['acceptance'], led.review_queue()), ('not_applicable', []))
        with self.assertRaisesRegex(ValueError, 'Failed checks'):
            led.apply_review(self.fixture, obs, 'accept')
        self.assertEqual([t['kind'] for t in led.transitions()], ['baseline'])

    def test_accepting_older_capture_requeues_later_auto_acceptance(self):
        led = self.ledger()
        self.baseline(led)
        changed, _ = self.capture(led, offer='$79 tune-up')
        later, row = self.capture(led, offer='$99 tune-up')   # equals baseline -> auto
        self.assertEqual(row['acceptance'], 'auto_accepted')
        led.apply_review(self.fixture, changed, 'accept')
        row = led.observation(later)
        self.assertEqual(row['acceptance'], 'pending_review')
        self.assertIn('baseline_changed_by_review', self.reasons(row))


class HumanGateTests(LedgerCase):
    def test_sessions_cannot_be_fabricated(self):
        with self.assertRaises(PermissionError):
            store.ReviewSession('Ray', 'interactive_cli', object())
        with self.assertRaises(PermissionError):
            store.interactive_session('Ray', stdin=io.StringIO(), stdout=io.StringIO())

    def test_fixture_sessions_refused_by_real_ledger(self):
        led = self.ledger('real-agency', synthetic=False)
        obs, _ = led.ingest(record(led.tenant, self.clock))
        with self.assertRaisesRegex(PermissionError, 'real ledger'):
            led.apply_review(self.fixture, obs, 'accept')
        self.assertEqual(led.observation(obs)['acceptance'], 'pending_review')

    def test_machine_and_fixture_never_labelled_human(self):
        led = self.ledger()
        self.baseline(led)
        self.capture(led)
        pages = led.page_states(iso(self.clock()), [])
        self.assertIn('Not individually human-reviewed', pages[0]['accepted_by'])
        self.assertNotIn('Human-reviewed', pages[0]['accepted_by'])
        self.assertEqual(led._rows("SELECT DISTINCT method FROM reviews")[0]['method'], 'synthetic_fixture')

    def test_interactive_session_records_person(self):
        tty = mock.Mock()
        tty.isatty.return_value = True
        session = store.interactive_session('Ray', stdin=tty, stdout=tty)
        led = self.ledger('real-agency', synthetic=False)
        obs, _ = led.ingest(record(led.tenant, self.clock))
        led.apply_review(session, obs, 'accept')
        self.assertEqual(led.observation(obs)['acceptance'], 'human_accepted')
        self.assertIn('Human-reviewed by Ray', led.page_states(iso(self.clock()), [])[0]['accepted_by'])


class DeliveryTests(LedgerCase):
    def test_lifecycle_and_unknown_delivery_flags_duplicates(self):
        led = self.ledger()
        self.baseline(led)
        out = Path(self.tmp.name) / 'r'
        rid, created = led.prepare_report(iso(self.clock()), out, report.render)
        self.assertEqual(led.prepare_report(iso(self.clock()), out, report.render), (rid, False))
        with self.assertRaises(ValueError):
            led.acknowledge_delivery(self.fixture, rid, 'manual')     # draft cannot be "delivered"
        led.release(self.fixture, rid)
        self.assertEqual(led.report(rid)['state'], 'released')        # released is not delivered
        led.mark_delivery_unknown(self.fixture, rid)
        self.clock.advance(days=7)
        rid2, _ = led.prepare_report(iso(self.clock()), out, report.render)
        content = json.loads(Path(led.report(rid2)['json_path']).read_text())
        self.assertEqual(content['events'][0]['possible_duplicate_of'], [rid])
        led.acknowledge_delivery(self.fixture, rid, 'found it in Sent items')
        self.clock.advance(days=7)
        rid3, _ = led.prepare_report(iso(self.clock()), out, report.render)
        self.assertEqual(json.loads(Path(led.report(rid3)['json_path']).read_text())['events'], [])

    def test_nothing_is_marked_sent_without_a_session(self):
        led = self.ledger()
        self.baseline(led)
        rid, _ = led.prepare_report(iso(self.clock()), Path(self.tmp.name) / 'r', report.render)
        for call in (lambda: led.release(None, rid), lambda: led.acknowledge_delivery(None, rid, 'x')):
            with self.assertRaises(PermissionError):
                call()
        self.assertEqual(led.report(rid)['state'], 'draft')

    def test_new_draft_voids_previous_unreleased_draft(self):
        led = self.ledger()
        self.baseline(led)
        out = Path(self.tmp.name) / 'r'
        r1, _ = led.prepare_report(iso(self.clock()), out, report.render)
        self.capture(led, days=1, offer='$79 tune-up')
        r2, _ = led.prepare_report(iso(self.clock()), out, report.render)
        self.assertEqual((led.report(r1)['state'], led.report(r2)['state']), ('void', 'draft'))


class StateAndRetentionTests(LedgerCase):
    def test_every_page_has_explicit_state_with_age(self):
        led = self.ledger()
        self.assertEqual(led.page_states(iso(self.clock()), [])[0]['state'], 'NOT CHECKED')
        self.baseline(led)
        _, _ = self.capture(led, missing=('financing',))
        page = led.page_states(iso(self.clock()), [])[0]
        self.assertEqual(page['state'], 'INCOMPLETE')
        self.assertEqual(page['accepted_age_days'], 3.0)
        self.capture(led, days=1, status='http_503')
        led2 = self.ledger('second-agency')
        self.baseline(led2)
        self.capture(led2, days=1, status='http_503')
        self.assertEqual(led2.page_states(iso(self.clock()), [])[0]['state'], 'CHECK FAILED')
        self.clock.advance(days=4)
        stale = led2.page_states(iso(self.clock()), [])[0]
        self.assertEqual(stale['state'], 'STALE')
        self.assertIn('http_503', stale['reason'])

    def test_prune_keeps_pending_and_ledger(self):
        led = self.ledger()
        raw = Path(self.tmp.name) / 'raw'
        rec = record(led.tenant, self.clock)
        rec['untrusted_source_text'] = 'page text'
        pending, _ = led.ingest(rec, raw_dir=raw)
        self.clock.advance(days=40)
        self.assertEqual(led.prune(store.DEFAULT_RETENTION, apply=True), [])   # still pending review
        led.apply_review(self.fixture, pending, 'accept')
        plan = led.prune(store.DEFAULT_RETENTION, apply=True)
        self.assertEqual(len(plan), 1)
        self.assertFalse((raw / f'{pending}.txt').exists())
        self.assertEqual(led.observation(pending)['acceptance'], 'fixture_accepted')
        self.assertEqual(len(led.transitions()), 1)

    def test_failed_write_rolls_back(self):
        led = self.ledger()
        rec = record(led.tenant, self.clock)
        with mock.patch.object(led, '_refresh', side_effect=RuntimeError('power cut')):
            with self.assertRaises(RuntimeError):
                led.ingest(rec)
        self.assertEqual(led._rows('SELECT COUNT(*) n FROM observations')[0]['n'], 0)
        self.assertTrue(led.ingest(rec)[1])


if __name__ == '__main__':
    unittest.main()
