"""Re-runs each review finding against the CURRENT code and prints what happens now.

Run from prototype/:  python -I review/repro_findings.py      (offline, no network)
The original reproductions against the supplied compare.py are in git history (commit f949d10).
"""
import copy
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import compare  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402
import capture  # noqa: E402
from owcore import iso  # noqa: E402


def page(pid='P', at='2026-10-06T16:00:00Z', facts=None, tenant='agency-a', **extra):
    record = {'tenant_id': tenant, 'id': pid, 'url': 'https://example.com/p', 'observed_at': at, 'status': 'ok',
              'facts': {'advertised_offer': '$79 tune-up'} if facts is None else facts,
              'reviewed': True, 'evidence_note': 'synthetic'}
    record.update(extra)
    return record


def snap(at, *pages, tenant='agency-a', **extra):
    return dict({'tenant_id': tenant, 'as_of': at, 'synthetic': True, 'pages': list(pages)}, **extra)


def status_of(before, after, pid='P'):
    return next(e for e in compare.compare(before, after)['events'] if e['page_id'] == pid)


def probe(name, fn):
    try:
        print(f'[{name}] {fn()}')
    except Exception as exc:
        print(f'[{name}] refused: {type(exc).__name__}: {exc}')


b0 = snap('2026-10-01T16:00:00Z', page(at='2026-10-01T16:00:00Z'))
probe('F1 presentation-only differences', lambda: [status_of(b0, snap('2026-10-06T16:00:00Z', page(
    facts={'advertised_offer': v})))['status'] for v in ('$79 tune-up ', '$79 tune-up', '＄79 tune-up')])
probe('F1 case (field-specific; advertised_offer is case-sensitive)', lambda: status_of(
    b0, snap('2026-10-06T16:00:00Z', page(facts={'advertised_offer': '$79 Tune-Up'})))['status'])
probe('F2 21-month-old baseline', lambda: (lambda e: (e['status'], e['flags'], e['reason']))(status_of(
    snap('2026-10-01T16:00:00Z', page(at='2025-01-01T00:00:00Z', facts={'advertised_offer': '$99'})),
    snap('2026-10-06T16:00:00Z', page()))))
probe('F3 empty facts on ok capture', lambda: status_of(b0, snap('2026-10-06T16:00:00Z', page(
    facts={}, missing_fields=['advertised_offer'])))['status'])
probe('F3b empty-string value', lambda: compare.validate(snap('2026-10-06T16:00:00Z', page(facts={'advertised_offer': ''}))))
probe('F4 cross-tenant compare', lambda: compare.compare(b0, snap('2026-10-06T16:00:00Z', page(tenant='agency-b'),
                                                                   tenant='agency-b')))
probe('F4c missing tenant', lambda: compare.validate({'as_of': '2026-10-06T16:00:00Z', 'synthetic': True, 'pages': []}))
n1 = snap('2026-10-06T16:00:00Z', page(facts={'advertised_offer': '$59'}))
n2 = copy.deepcopy(n1)
n2['pages'][0]['evidence_note'] = 'typo fixed'
probe('F5 note edit changes event_id', lambda: status_of(b0, n1)['event_id'] != status_of(b0, n2)['event_id'])
probe('F6 automated capture marked reviewed without attribution', lambda: compare.validate(
    snap('2026-10-06T16:00:00Z', page(capture_method='automated'))))
probe('F7 synthetic before + real after', lambda: compare.compare(b0, dict(snap('2026-10-06T16:00:00Z', page()),
                                                                           synthetic=False)))
probe('F10 missing facts key', lambda: compare.validate(snap('2026-10-06T16:00:00Z', {
    k: v for k, v in page().items() if k != 'facts'})))
probe('F11b render() with javascript: URL', lambda: 'href="javascript:' in compare.render(
    {'tenant_id': 'agency-a', 'as_of': 'x', 'synthetic': False, 'events': [
        {'tenant_id': 'agency-a', 'status': 's', 'page_id': 'p', 'details': [], 'observed_at': None,
         'age_hours': None, 'evidence_note': '', 'url': 'javascript:alert(1)'}]}))
probe('F14 output out.json', lambda: compare.output_paths('out.json', ['a.json', 'b.json']))
probe('P1 pending review visible in render', lambda: 'PENDING REVIEW' in compare.render(compare.compare(
    b0, snap('2026-10-06T16:00:00Z', page(at='2026-10-03T16:00:00Z'), pending_review=['P']))))


def ledger_probe():
    """F4/F5b in the persistent pipeline: same URL in two tenants, repeat delivery across weeks."""
    with tempfile.TemporaryDirectory() as tmp:
        from datetime import datetime, timezone
        clock_value = [datetime(2026, 10, 1, 9, tzinfo=timezone.utc)]
        clock = lambda: clock_value[0]
        fixture, ids, outcome = store.synthetic_session(), {}, {}
        for tenant in ('agency-a', 'agency-b'):
            clock_value[0] = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
            allow = capture.load_allowlist({
                'tenant_id': tenant, 'synthetic': True, 'user_agent': 'OfferWatchBot/0.2 (+x)',
                'pages': [{'id': 'P', 'url': 'https://example.com/p', 'approved_by': 'c', 'approved_at': '2026-10-01T00:00:00Z',
                           'fields': {'advertised_offer': {'pattern': '(x)'}}}]})
            led = store.Ledger(Path(tmp) / tenant / 'l.sqlite3', tenant, True, clock)
            led.sync_sources(allow, capture.access_attested)
            for day, offer in ((1, '$99'), (4, '$79'), (8, '$79')):     # change, then unchanged repeat
                clock_value[0] = datetime(2026, 10, day, 9, tzinfo=timezone.utc)
                obs, _ = led.ingest({'tenant_id': tenant, 'id': 'P', 'url': 'https://example.com/p',
                                     'observed_at': iso(clock()), 'status': 'ok', 'capture_method': 'automated',
                                     'raw_facts': {'advertised_offer': offer}, 'facts': {'advertised_offer': offer},
                                     'text_length': 100})
                if led.observation(obs)['acceptance'] == 'pending_review':
                    led.apply_review(fixture, obs, 'accept')
            rid, _ = led.prepare_report(iso(clock()), Path(tmp) / 'r', report.render)
            led.release(fixture, rid)
            led.acknowledge_delivery(fixture, rid, 'rehearsal')
            clock_value[0] = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)
            rid2, _ = led.prepare_report(iso(clock()), Path(tmp) / 'r', report.render)
            outcome[tenant] = len([1 for i in led._rows('SELECT * FROM report_items WHERE report_id=?', rid2)])
            ids[tenant] = {t['transition_id'] for t in led.transitions()}
            led.close()
        return {'shared transition ids across tenants': len(ids['agency-a'] & ids['agency-b']),
                'items repeated in next week after acknowledged delivery': outcome}


probe('F4/F5b persistent ledger', ledger_probe)
