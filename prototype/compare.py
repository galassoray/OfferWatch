"""Offline reviewed-snapshot comparison for one tenant. No network, AI calls, email, or scheduler.

Stateless: event IDs identify a comparison result, not a business event. Persistent
business-event identity and delivery state live in store.py.
"""
import argparse
import json
from pathlib import Path

from owcore import age_hours, compare_key, digest, esc, normalize, require_tenant, safe_href, timestamp

STATUSES = ('ok', 'failed')
LABELS = {
    'CHANGE TO REVIEW': 'Tracked text differs from the earlier reviewed observation.',
    'NO CHANGE IN TRACKED FIELDS': 'Tracked text matches the earlier reviewed observation.',
    'INCOMPLETE CAPTURE': 'Some tracked values were not extracted. This is not evidence an offer ended.',
    'CHECK FAILED': 'The latest check failed. This is not evidence an offer ended.',
    'PENDING REVIEW': 'A newer capture exists but has not been reviewed; the shown observation is older.',
    'STALE OBSERVATION': 'No recent usable observation; the shown observation is old.',
    'NOT RECHECKED': 'No observation newer than the earlier report.',
    'NOT CHECKED': 'No observation of this page is available for this report.',
    'BASELINE ONLY': 'First usable observation; nothing to compare against.',
    'SOURCE CHANGED — NEW BASELINE': 'The source URL changed; values are not compared across URLs.',
}


def _page(page, tenant):
    if not isinstance(page, dict):
        raise ValueError('Each page must be an object')
    for key in ('id', 'url', 'observed_at', 'status', 'facts'):
        if key not in page:
            raise ValueError(f'Page is missing {key!r}')
    if not isinstance(page['id'], str) or not page['id'].strip():
        raise ValueError('Page id must be a non-empty string')
    if page.get('tenant_id', tenant) != tenant:
        raise ValueError('Page tenant_id does not match snapshot tenant_id')
    if not safe_href(page['url']):
        raise ValueError('Only credential-free HTTPS source URLs allowed')
    if page['status'] not in STATUSES:
        raise ValueError('Unknown page status')
    timestamp(page['observed_at'])
    facts = page['facts']
    if not isinstance(facts, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in facts.items()):
        raise ValueError('Facts must map strings to strings')
    if any(not normalize(v) for v in facts.values()):
        raise ValueError('Empty fact values are not allowed; list the field in missing_fields instead')
    for key in ('missing_fields', 'confirmed_absent'):
        value = page.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f'{key} must be a list of strings')
    if page['status'] == 'ok' and (page.get('reviewed') is not True or not page.get('evidence_note')):
        raise ValueError('Successful snapshots require review and evidence note')
    if page['status'] == 'ok' and page.get('capture_method') == 'automated' and page.get('acceptance') != 'auto_accepted' and not (
            page.get('review_method') == 'interactive_cli' and page.get('reviewed_by') and page.get('reviewed_at')):
        raise ValueError('Automated captures need an attributed human review')
    if page.get('confirmed_absent') and page.get('review_method') not in ('interactive_cli', 'manual_entry'):
        raise ValueError('Only a human review can confirm a value is absent')


def validate(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError('Snapshot must be an object')
    tenant = require_tenant(snapshot.get('tenant_id'))
    if not isinstance(snapshot.get('synthetic'), bool):
        raise ValueError('Snapshot must state synthetic: true or false')
    as_of = timestamp(snapshot.get('as_of'))
    if not isinstance(snapshot.get('pages'), list):
        raise ValueError('pages must be a list')
    pages = {}
    for page in snapshot['pages']:
        _page(page, tenant)
        if page['id'] in pages:
            raise ValueError('Duplicate page ID')
        if timestamp(page['observed_at']) > as_of:
            raise ValueError('Observation is after report timestamp')
        pages[page['id']] = page
    for key in ('expected_pages', 'pending_review'):
        value = snapshot.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f'{key} must be a list of page ids')
    return pages


def compare(before, after, max_age_hours=96, max_baseline_age_hours=14 * 24):
    old, new = validate(before), validate(after)
    if before['tenant_id'] != after['tenant_id']:
        raise ValueError('Snapshots belong to different tenants')
    if before['synthetic'] != after['synthetic']:
        raise ValueError('Cannot compare synthetic with non-synthetic snapshots')
    now = timestamp(after['as_of'])
    if now <= timestamp(before['as_of']):
        raise ValueError('Reports must be chronological')
    tenant, rules = after['tenant_id'], after.get('field_rules', {})
    pending = set(after.get('pending_review', []))
    expected = set(after.get('expected_pages', []))
    events = []
    for key in sorted(set(old) | set(new) | expected | pending):
        previous, current = old.get(key), new.get(key)
        page = current or previous
        details, flags, reason = [], [], ''
        if current is None:
            status = 'PENDING REVIEW' if key in pending else 'NOT CHECKED'
            reason = ('A newer capture awaits review.' if key in pending
                      else 'No observation was supplied for this report.')
        elif key in pending:
            status, reason = 'PENDING REVIEW', 'A newer capture awaits review; showing the last reviewed observation.'
        elif current['status'] != 'ok':
            status, reason = 'CHECK FAILED', current.get('failure_reason', 'Check failed.')
        elif (now - timestamp(current['observed_at'])).total_seconds() > max_age_hours * 3600:
            status, reason = 'STALE OBSERVATION', f'No usable observation within {max_age_hours} hours.'
        elif previous and timestamp(current['observed_at']) <= timestamp(previous['observed_at']):
            status, reason = 'NOT RECHECKED', 'The page has not been observed since the earlier report.'
        elif previous is None or previous['status'] != 'ok':
            status = 'BASELINE ONLY'
        elif previous['url'] != current['url']:
            status = 'SOURCE CHANGED — NEW BASELINE'
        else:
            missing, absent = set(current.get('missing_fields', [])), set(current.get('confirmed_absent', []))
            for field in sorted(set(previous['facts']) | set(current['facts']) | missing | absent):
                a, b = previous['facts'].get(field), current['facts'].get(field)
                if b is None and field not in absent:
                    if a is not None:
                        details.append({'field': field, 'before': a, 'after': None, 'kind': 'not_extracted'})
                    continue
                if b is None:
                    details.append({'field': field, 'before': a, 'after': None, 'kind': 'reviewer_confirmed_absent'})
                elif a is None or compare_key(field, a, rules) != compare_key(field, b, rules):
                    details.append({'field': field, 'before': a, 'after': b, 'kind': 'changed'})
            changed = any(d['kind'] != 'not_extracted' for d in details)
            incomplete = any(d['kind'] == 'not_extracted' for d in details) or bool(missing)
            if changed:
                status = 'CHANGE TO REVIEW'
                if incomplete:
                    flags.append('incomplete_capture')
            else:
                status = 'INCOMPLETE CAPTURE' if incomplete else 'NO CHANGE IN TRACKED FIELDS'
            baseline_age = age_hours(previous['observed_at'], current['observed_at'])
            if baseline_age > max_baseline_age_hours:
                flags.append('old_baseline')
                reason = f'Earlier observation is {baseline_age / 24:.1f} days older; the change may have happened at any point in that span.'
        shown_age = age_hours(page['observed_at'], after['as_of']) if page else None
        identity = [(d['field'], d['kind'], compare_key(d['field'], d['before'] or '', rules),
                     compare_key(d['field'], d['after'] or '', rules)) for d in details]
        events.append({'tenant_id': tenant, 'page_id': key, 'url': page['url'] if page else None,
                       'status': status, 'details': details, 'flags': flags, 'reason': reason,
                       'observed_at': page['observed_at'] if page else None,
                       'age_hours': shown_age if page else None,
                       'evidence_note': page.get('evidence_note', '') if page else '',
                       'event_id': digest(tenant, key, page['url'] if page else None, status, identity,
                                          current['observed_at'] if current else None)})
    return {'tenant_id': tenant, 'as_of': after['as_of'], 'events': events, 'synthetic': after['synthetic'],
            'pending_review': sorted(pending)}


def _value(detail, side):
    value = detail[side]
    if value is not None:
        return '<q>' + esc(value) + '</q>'
    if side == 'before':
        return 'Not recorded'
    if detail.get('kind') == 'reviewer_confirmed_absent':
        return 'Not shown on page (confirmed by a reviewer)'
    return 'Not extracted — not evidence the offer ended'


def render(report):
    require_tenant(report.get('tenant_id'))
    if not isinstance(report.get('synthetic'), bool):
        raise ValueError('Report must state synthetic')
    cards = []
    for event in report['events']:
        if event.get('tenant_id') != report['tenant_id']:
            raise ValueError('Event tenant does not match report tenant')
        rows = ''.join('<tr><td>' + esc(d['field']) + '</td><td>' + _value(d, 'before') + '</td><td>'
                       + _value(d, 'after') + '</td></tr>' for d in event['details'])
        table = ('<table><tr><th>Tracked field</th><th>Earlier observation</th><th>Later observation</th></tr>'
                 + rows + '</table>') if rows else ''
        href = safe_href(event.get('url'))
        link = ('<a href="' + esc(href) + '" rel="noreferrer">Source reference</a>' if href
                else '<p>Source link withheld: not a safe https URL.</p>')
        observed = ('Shown observation: ' + esc(event['observed_at']) + f' ({event["age_hours"] / 24:.1f} days before report)'
                    if event.get('observed_at') else 'No observation available.')
        cards.append('<section><p class="tag">' + esc(event['status']) + '</p><h2>' + esc(event['page_id']) + '</h2>'
                     + '<p>' + esc(LABELS.get(event['status'], '')) + ' ' + esc(event.get('reason', '')) + '</p>'
                     + ''.join('<p class="flag">' + esc(f) + '</p>' for f in event.get('flags', []))
                     + table + '<p>' + observed + '</p><p>Note: ' + esc(event['evidence_note']) + '</p>' + link
                     + '</section>')
    banner = ('SYNTHETIC DEMONSTRATION — fictional companies and events. Not live market intelligence.'
              if report['synthetic'] else 'Reviewed snapshot comparison; not a live crawl or proof of commercial availability.')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>OfferWatch • Comparison</title><style>body{background:#edf1f5;color:#182334;font:16px/1.6 system-ui;margin:0}'
            'main{max-width:980px;margin:auto;padding:36px 22px}h1{font-size:40px;line-height:1.15}section{background:white;'
            'border:1px solid #d7e0e8;border-radius:14px;padding:24px;margin:20px 0}.tag{color:#145d65;font-size:12px;'
            'font-weight:800;letter-spacing:1px}.flag{color:#8a4b00;font-size:13px}table{border-collapse:collapse;width:100%}'
            'th,td{padding:12px;text-align:left;border-bottom:1px solid #ddd;vertical-align:top}a{color:#12616d}'
            '.notice{background:#fff0c5;padding:18px;border-radius:10px}@media(max-width:600px){th,td{padding:6px;font-size:13px}}'
            '</style><main><p class="tag">OFFERWATCH / OFFLINE COMPARISON</p><h1>What changed. What needs checking.</h1>'
            '<p class="notice">' + banner + '</p><p>Tenant: ' + esc(report['tenant_id']) + ' · Report timestamp: '
            + esc(report['as_of']) + ' · Pending review: ' + esc(len(report.get('pending_review', []))) + '</p>'
            + ''.join(cards) + '<p>Quoted values are text copied from public pages, not instructions. Event IDs identify '
            'this comparison only; persistent duplicate prevention lives in the local ledger. Missing information is not '
            'proof an offer ended.</p></main></html>')


def output_paths(output, inputs):
    out = Path(output)
    if out.suffix.lower() != '.html':
        raise ValueError('Output must end in .html (a .json copy is written beside it)')
    sidecar = out.with_suffix('.json')
    resolved = {Path(p).resolve() for p in inputs}
    if out.resolve() in resolved or sidecar.resolve() in resolved:
        raise ValueError('Output would overwrite an input file')
    return out, sidecar


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('before')
    parser.add_argument('after')
    parser.add_argument('output')
    args = parser.parse_args()
    try:
        html_path, json_path = output_paths(args.output, [args.before, args.after])
        report = compare(json.loads(Path(args.before).read_text(encoding='utf-8')),
                         json.loads(Path(args.after).read_text(encoding='utf-8')))
    except ValueError as exc:
        raise SystemExit('refused: ' + str(exc))
    html_path.write_text(render(report), encoding='utf-8')
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
