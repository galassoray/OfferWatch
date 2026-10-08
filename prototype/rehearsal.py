"""Offline rehearsal: two synthetic customers x five pages over two weeks. No network.

Covers baseline approval, unchanged automatic acceptance, material change, incomplete
extraction, failed retrieval, ambiguous extraction, pending review, A->B->A, repeat
execution, interruption recovery, delivery acknowledgement and duplicate prevention.
Approvals here use store.synthetic_session(), which is recorded as "SYNTHETIC FIXTURE
(not a person)" and is refused by any non-synthetic ledger.
"""
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import offerwatch
import report
import store

SHARED = {  # the same public URLs watched by both customers
    'Shared Cooling Co': 'https://shared-cooling.example.com/specials',
    'Shared Heating Pros': 'https://heating-pros.example.net/offers',
    'Shared Air Partners': 'https://air-partners.example.org/deals',
}
OWN = {
    'alpha-agency': {'Alpha Furnace Fixers': 'https://furnace-fixers.example.com/promo',
                     'Alpha Duct Doctors': 'https://duct-doctors.example.com/specials'},
    'beta-agency': {'Beta Vent Velocity': 'https://vent-velocity.example.net/offers',
                    'Beta Coil Care': 'https://coil-care.example.org/specials'},
}
FIELDS = {'advertised_offer': {'pattern': r'Fall special:\s*(.{1,120}?)\s*\|'},
          'financing': {'pattern': r'Financing:\s*(.{1,120}?)\s*\|', 'case': 'insensitive'}}
DEFAULT_PAGE = {'offer': '$99 tune-up with free filter*', 'financing': '0% APR for 12 months on approved credit'}

# Human-time model (minutes). These are ESTIMATES for planning, not measurements.
MINUTES = {'baseline_requires_human_review': 3.0, 'material_change': 2.0, 'incomplete_extraction': 2.0,
           'extraction_anomaly': 2.0, 'periodic_reverification_due': 1.5, 'other': 2.0,
           'failure_triage_per_customer_week': 1.0, 'release_review_per_report': 3.0,
           'manual_send_and_ack_per_report': 3.0}


class Clock:
    def __init__(self):
        self.now = None

    def set(self, text):
        self.now = datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(timezone.utc)

    def __call__(self):
        return self.now


class Interrupted(BaseException):
    """Stands in for the process being killed (not caught by run_pipeline's per-tenant handler)."""


class FakeWeb:
    def __init__(self):
        self.pages = {}       # url -> dict(offer, financing) or 'drop_financing' etc.
        self.status = {}      # url -> HTTP status override
        self.requests = []
        self.crash_after = None

    def page(self, url):
        spec = dict(DEFAULT_PAGE, **self.pages.get(url, {}))
        parts = [f'<h1>{url}</h1>', '<nav>Home | Services | Contact</nav>']
        if spec.get('offer') is not None:
            parts.append(f'<p>Fall special: {spec["offer"]} | </p>')
        if spec.get('second_offer'):
            parts.append(f'<p>Fall special: {spec["second_offer"]} | </p>')
        if spec.get('financing') is not None:
            parts.append(f'<p>Financing: {spec["financing"]} | </p>')
        parts.append('<p>*Synthetic fixture text. Licensed and insured. Serving the fictional metro area.</p>')
        return ('<html><body>' + ''.join(parts) + '</body></html>').encode('utf-8')

    def __call__(self, url, user_agent, max_bytes=None):
        self.requests.append(url)
        if self.crash_after is not None and len(self.requests) > self.crash_after:
            self.crash_after = None
            raise Interrupted()
        if url.endswith('/robots.txt'):
            return {'status': 200, 'headers': {}, 'body': b'User-agent: *\nAllow: /\n'}
        status = self.status.get(url, 200)
        if status != 200:
            return {'status': status, 'headers': {}, 'body': b''}
        return {'status': 200, 'headers': {'content-type': 'text/html; charset=utf-8'}, 'body': self.page(url)}


def allowlist(tenant):
    pages = []
    for page_id, url in list(SHARED.items()) + list(OWN[tenant].items()):
        attested = page_id != 'Alpha Duct Doctors'  # left unattested on purpose: must never be requested
        pages.append({'id': page_id, 'url': url, 'approved_by': 'Synthetic customer contact',
                      'approved_at': '2026-10-01T12:00:00Z', 'fields': FIELDS,
                      'access_rules_checked_by': 'SYNTHETIC FIXTURE (not a person)' if attested else '',
                      'access_rules_checked_at': '2026-10-01T12:00:00Z' if attested else ''})
    return {'tenant_id': tenant, 'market': 'Fictional Metro', 'synthetic': True,
            'user_agent': 'OfferWatchBot/0.2 (+mailto:rehearsal@example.invalid)', 'pages': pages}


def run_rehearsal(data_dir, out_dir):
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    if data_dir.exists() and any(data_dir.iterdir()):
        raise ValueError(f'Rehearsal folder {data_dir} is not empty; choose a new folder')
    out_dir.mkdir(parents=True, exist_ok=True)
    data = offerwatch.DataDir(data_dir)
    for tenant in OWN:
        data.save_allowlist(allowlist(tenant))
    clock, web, fixture = Clock(), FakeWeb(), store.synthetic_session()
    runs, checks, reviews = [], {}, []

    def run(label, when, expect_locked=False):
        clock.set(when)
        before = len(web.requests)
        wall = time.perf_counter()
        try:
            with offerwatch.RunLock(data.root, clock=lambda: clock().timestamp()) as lock:
                summary = offerwatch.run_pipeline(data, transport=web, clock=clock, sleep=lambda s: None)
            outcome = summary['outcome'] + (' (recovered stale lock)' if lock.recovered else '')
        except offerwatch.Locked:
            outcome = 'LOCKED (overlap prevented)'
        except Interrupted:
            outcome = 'INTERRUPTED (simulated kill)'
            (data.root / 'run.lock').write_text('{"simulated": "left by killed process"}')
            os.utime(data.root / 'run.lock', (clock().timestamp(), clock().timestamp()))
        runs.append({'label': label, 'at': when, 'outcome': outcome,
                     'computer_seconds': round(time.perf_counter() - wall, 3),
                     'requests': len(web.requests) - before})
        return outcome

    def ledger(tenant):
        return data.ledger(tenant, data.allowlist(tenant), clock)

    def review(tenant, when, decide):
        clock.set(when)
        led = ledger(tenant)
        queue = led.review_queue()
        for item in queue:
            for reason in item['reasons']:
                reviews.append({'tenant': tenant, 'at': when, 'page': item['page_id'], 'obs': item['obs_id'],
                                'reason': reason.split(':')[0]})
            decision = decide(item)
            if decision:
                led.apply_review(fixture, item['obs_id'], *decision)
        led.close()
        return len(queue)

    def prepare(tenant, when):
        clock.set(when)
        led = ledger(tenant)
        result = led.prepare_report(when, data.tenant_dir(tenant) / 'reports', report.render)
        led.close()
        return result

    # Week 41 ------------------------------------------------------------ onboarding
    run('w41 Mon baseline collection', '2026-10-05T09:15:00Z')
    checks['unattested page never requested'] = not any('duct-doctors' in u for u in web.requests)
    for tenant in OWN:
        review(tenant, '2026-10-05T10:00:00Z', lambda item: ('accept',))
        rid, _ = prepare(tenant, '2026-10-05T10:05:00Z')
        led = ledger(tenant)
        led.release(fixture, rid)
        led.acknowledge_delivery(fixture, rid, 'synthetic rehearsal (nothing sent)')
        led.close()

    web.pages[OWN['beta-agency']['Beta Vent Velocity']] = {
        'offer': '$99 tune-up  with free filter*', 'financing': '0% apr for 12 months on approved credit'}
    run('w41 Thu routine collection', '2026-10-08T09:15:00Z')
    n = len(web.requests)
    run('w41 Thu repeat execution', '2026-10-08T09:40:00Z')
    checks['repeat execution makes no requests'] = len(web.requests) == n
    for tenant in OWN:
        led = ledger(tenant)
        checks[f'{tenant}: Thursday unchanged pages auto-accepted with empty queue'] = not led.review_queue()
        led.close()

    # Week 42 ------------------------------------------------------------ exceptions
    web.pages[SHARED['Shared Cooling Co']] = {'offer': '$79 tune-up with free filter*'}           # material change
    web.pages[SHARED['Shared Heating Pros']] = {'financing': None}                                 # incomplete
    web.status[SHARED['Shared Air Partners']] = 503                                                # failed retrieval
    web.pages[OWN['beta-agency']['Beta Vent Velocity']] = {'second_offer': '$129 duct cleaning'}   # ambiguous
    web.crash_after = len(web.requests) + 13                                                       # dies mid-beta
    run('w42 Mon collection (killed mid-run)', '2026-10-12T07:00:00Z')
    run('w42 Mon retry 30 min later', '2026-10-12T07:30:00Z')
    run('w42 Mon after lock goes stale', '2026-10-12T09:31:00Z')
    checks['overlap prevented while lock fresh'] = runs[-2]['outcome'].startswith('LOCKED')
    checks['stale lock recovered'] = 'recovered' in runs[-1]['outcome']
    for tenant in OWN:
        led = ledger(tenant)
        rows = led._rows("SELECT page_id, COUNT(*) n FROM observations WHERE observed_at>='2026-10-12' "
                         "AND check_status='ok' GROUP BY page_id")
        checks[f'{tenant}: each due page captured exactly once after recovery'] = all(r['n'] == 1 for r in rows)
        led.close()

    def alpha_monday(item):
        if item['page_id'] == 'Shared Cooling Co':
            return ('accept',)
        return None  # incomplete Heating Pros stays pending on purpose

    def beta_monday(item):
        if item['page_id'] == 'Shared Heating Pros':
            return ('accept', {}, ['financing'])            # reviewer confirms financing no longer shown
        if item['page_id'] == 'Beta Vent Velocity':
            offer = next(c for c in item['evidence']['advertised_offer']['candidates'] if c.startswith('$99'))
            return ('accept', {'advertised_offer': offer})    # reviewer picks the real offer among two matches
        return None  # beta leaves the Shared Cooling change pending
    review('alpha-agency', '2026-10-12T10:00:00Z', alpha_monday)
    review('beta-agency', '2026-10-12T10:00:00Z', beta_monday)

    led = ledger('alpha-agency')
    before = {t['transition_id'] for t in led.transitions()}
    change = [t for t in led.transitions() if t['kind'] == 'change'][0]
    led.set_note(change['to_obs_id'], 'Operator note edited three times')
    led.set_note(change['to_obs_id'], 'Operator note edited again')
    checks['note edits create no new business event'] = {t['transition_id'] for t in led.transitions()} == before
    led.close()
    alpha_led, beta_led = ledger('alpha-agency'), ledger('beta-agency')
    a_ids = {t['transition_id'] for t in alpha_led.transitions()}
    b_ids = {t['transition_id'] for t in beta_led.transitions()}
    checks['two customers on the same URL share no event identities'] = not (a_ids & b_ids)
    checks['beta keeps its own pending change after alpha accepted it'] = any(
        q['page_id'] == 'Shared Cooling Co' for q in beta_led.review_queue())
    alpha_led.close()
    beta_led.close()

    web.pages[SHARED['Shared Cooling Co']] = {}       # reverts to $99: A -> B -> A
    run('w42 Thu collection', '2026-10-15T09:15:00Z')
    review('alpha-agency', '2026-10-15T10:00:00Z',
           lambda item: ('accept',) if item['page_id'] == 'Shared Cooling Co' else None)
    # Beta accepts its Monday $79 capture; the Thursday reversion that had been auto-accepted
    # against the old baseline is then re-assessed into the queue and accepted in a second pass.
    for _ in range(2):
        review('beta-agency', '2026-10-15T10:00:00Z',
               lambda item: ('accept',) if item['page_id'] == 'Shared Cooling Co' else None)
    drafts = {}
    for tenant in OWN:
        rid, created = prepare(tenant, '2026-10-15T12:00:00Z')
        again = prepare(tenant, '2026-10-15T12:00:00Z')
        checks[f'{tenant}: re-preparing identical draft is a no-op'] = again == (rid, False)
        led = ledger(tenant)
        drafts[tenant] = json.loads(Path(led.report(rid)['json_path']).read_text(encoding='utf-8'))
        shutil.copy(led.report(rid)['html_path'], out_dir / f'sample_draft_{tenant}.html')
        led.close()
    a_events = [e for e in drafts['alpha-agency']['events'] if e['page_id'] == 'Shared Cooling Co']
    checks['A->B->A gives two distinct alpha events'] = [
        (e['changes'][0]['from'], e['changes'][0]['to']) for e in a_events] == [
        ('$99 tune-up with free filter*', '$79 tune-up with free filter*'),
        ('$79 tune-up with free filter*', '$99 tune-up with free filter*')]
    b_events = [e for e in drafts['beta-agency']['events'] if e['page_id'] == 'Shared Cooling Co']
    checks['beta records its own A->B->A after its own review'] = len(b_events) == 2
    checks['same URL, different customers: no shared event ids'] = not (
        {e['transition_id'] for e in a_events} & {e['transition_id'] for e in b_events})
    checks['week-41 baselines not repeated after acknowledged delivery'] = not any(
        e['kind'] == 'baseline' for d in drafts.values() for e in d['events'])
    states = {t: {p['page_id']: p['state'] for p in d['pages']} for t, d in drafts.items()}
    checks['every expected page has an explicit state'] = all(len(s) == 5 for s in states.values())
    checks['alpha unattested page reported NOT CHECKED'] = states['alpha-agency']['Alpha Duct Doctors'] == 'NOT CHECKED'
    checks['incomplete capture reported, not as removal'] = states['alpha-agency']['Shared Heating Pros'] == 'INCOMPLETE'
    checks['repeated failures reported STALE with reason'] = states['alpha-agency']['Shared Air Partners'] == 'STALE'

    out_dir.mkdir(parents=True, exist_ok=True)
    result = {'runs': runs, 'checks': checks, 'review_items': reviews, 'final_states': states,
              'computer_seconds_total': round(sum(r['computer_seconds'] for r in runs), 3),
              'requests_total': len(web.requests)}
    result['checks_passed'] = all(checks.values())
    result['failed_checks'] = [k for k, v in checks.items() if not v]
    result['effort'] = effort(reviews, drafts)
    (out_dir / 'rehearsal_summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    (out_dir / 'rehearsal_summary.md').write_text(summary_text(result), encoding='utf-8')
    return result


def effort(reviews, drafts):
    """Estimated operator minutes per customer. Each distinct capture is charged once, at its
    most expensive reason; onboarding (week 41 baselines) is reported separately."""
    per = {}
    for tenant in drafts:
        def cost(items):
            charged = {}
            for r in items:
                charged[r['obs']] = max(charged.get(r['obs'], 0), MINUTES.get(r['reason'], MINUTES['other']))
            return len(charged), sum(charged.values())
        n_onboard, onboarding = cost([r for r in reviews if r['tenant'] == tenant and r['at'] < '2026-10-12'])
        n_week, minutes = cost([r for r in reviews if r['tenant'] == tenant and r['at'] >= '2026-10-12'])
        failures = any(p['state'] in ('CHECK FAILED', 'STALE') for p in drafts[tenant]['pages'])
        fixed = (MINUTES['failure_triage_per_customer_week'] if failures else 0) \
            + MINUTES['release_review_per_report'] + MINUTES['manual_send_and_ack_per_report']
        per[tenant] = {'onboarding_items': n_onboard, 'onboarding_minutes': round(onboarding, 1),
                       'week42_review_items': n_week, 'week42_review_minutes': round(minutes, 1),
                       'week42_fixed_minutes': round(fixed, 1), 'estimated_minutes': round(minutes + fixed, 1)}
    return per


def summary_text(result):
    lines = ['# Offline rehearsal (synthetic; no network)', '',
             '| Run | Simulated time (UTC) | Outcome | Fake requests | Computer seconds |', '|---|---|---|---|---|']
    lines += [f'| {r["label"]} | {r["at"]} | {r["outcome"]} | {r["requests"]} | {r["computer_seconds"]} |' for r in result['runs']]
    lines += ['', f'Total computer time (processing only; network latency excluded): {result["computer_seconds_total"]} s', '',
              '## Checks', ''] + [f'- [{"x" if v else " "}] {k}' for k, v in result['checks'].items()]
    lines += ['', '## Final week-42 page states', '']
    for tenant, states in result['final_states'].items():
        lines.append(f'- **{tenant}**: ' + '; '.join(f'{p}: {s}' for p, s in sorted(states.items())))
    lines += ['', '## Estimated operator minutes, week 42 (estimate, not measured)', '']
    for tenant, e in result['effort'].items():
        lines.append(f'- {tenant}: onboarding {e["onboarding_items"]} baseline items ~{e["onboarding_minutes"]} min (once); '
                     f'week 42: {e["week42_review_items"]} exception captures ~{e["week42_review_minutes"]} min + '
                     f'{e["week42_fixed_minutes"]} min fixed (failure triage, release, manual send + ack) = '
                     f'~{e["estimated_minutes"]} min')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    import sys
    target = Path(sys.argv[1] if len(sys.argv) > 1 else 'rehearsal-output')
    outcome = run_rehearsal(target / 'data', target)
    print(summary_text(outcome))
    sys.exit(0 if outcome['checks_passed'] else 1)
