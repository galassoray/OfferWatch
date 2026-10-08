"""Offline reviewed-snapshot comparison. No network, AI calls, email, or scheduler."""
import argparse
import hashlib
import html
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return result


def validate(snapshot):
    timestamp(snapshot['as_of'])
    pages = {}
    for page in snapshot['pages']:
        key = page['id']
        if key in pages:
            raise ValueError('Duplicate page ID')
        url = urlsplit(page['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('Only credential-free HTTPS source URLs allowed')
        if page['status'] not in ('ok', 'failed'):
            raise ValueError('Unknown page status')
        if timestamp(page['observed_at']) > timestamp(snapshot['as_of']):
            raise ValueError('Observation is after report timestamp')
        if not isinstance(page['facts'], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in page['facts'].items()):
            raise ValueError('Facts must map strings to strings')
        if page['status'] == 'ok' and (page.get('reviewed') is not True or not page.get('evidence_note')):
            raise ValueError('Successful snapshots require review and evidence note')
        if page['status'] == 'ok' and page.get('capture_method') == 'automated' and not (
                page.get('review_method') == 'interactive_cli' and page.get('reviewed_by') and page.get('reviewed_at')):
            raise ValueError('Automated captures need an attributed human review')
        pages[key] = page
    return pages


def compare(before, after, max_age_hours=96):
    old, new = validate(before), validate(after)
    now = timestamp(after['as_of'])
    if now <= timestamp(before['as_of']):
        raise ValueError('Reports must be chronological')
    events = []
    for key in sorted(set(old) | set(new)):
        previous, current = old.get(key), new.get(key)
        page = current or previous
        details = []
        if current is None:
            status = 'NOT CHECKED'
        elif current['status'] != 'ok':
            status = 'CHECK FAILED'
        elif (now - timestamp(current['observed_at'])).total_seconds() > max_age_hours * 3600:
            status = 'STALE OBSERVATION'
        elif previous and timestamp(current['observed_at']) <= timestamp(previous['observed_at']):
            status = 'NOT RECHECKED'
        elif previous is None or previous['status'] != 'ok':
            status = 'BASELINE ONLY'
        elif previous['url'] != current['url']:
            status = 'SOURCE CHANGED — NEW BASELINE'
        else:
            for field in sorted(set(previous['facts']) | set(current['facts'])):
                a, b = previous['facts'].get(field), current['facts'].get(field)
                if a != b:
                    details.append({'field': field, 'before': a, 'after': b})
            status = 'CHANGE TO REVIEW' if details else 'NO CHANGE IN TRACKED FIELDS'
        payload = {'page_id': key, 'url': page['url'], 'status': status, 'details': details,
                   'observed_at': page['observed_at'], 'evidence_note': page.get('evidence_note', '')}
        payload['event_id'] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
        events.append(payload)
    return {'as_of': after['as_of'], 'events': events, 'synthetic': after.get('synthetic', False)}


def render(report):
    esc = lambda x: html.escape(str(x))
    cards = []
    for event in report['events']:
        rows = ''.join('<tr><td>'+esc(d['field'])+'</td><td>'+esc(d['before'] if d['before'] is not None else 'Not recorded')+'</td><td>'+esc(d['after'] if d['after'] is not None else 'Not recorded — verify before claiming removal')+'</td></tr>' for d in event['details'])
        table = '<table><tr><th>Tracked field</th><th>Earlier observation</th><th>Later observation</th></tr>'+rows+'</table>' if rows else ''
        cards.append('<section><p class="tag">'+esc(event['status'])+'</p><h2>'+esc(event['page_id'])+'</h2>'+table+'<p>Observed: '+esc(event['observed_at'])+'</p><p>'+esc(event['evidence_note'])+'</p><a href="'+esc(event['url'])+'" rel="noreferrer">Source reference</a></section>')
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OfferWatch • Comparison demonstration</title><style>body{background:#edf1f5;color:#182334;font:16px/1.6 system-ui;margin:0}main{max-width:980px;margin:auto;padding:36px 22px}h1{font-size:40px;line-height:1.15}section{background:white;border:1px solid #d7e0e8;border-radius:14px;padding:24px;margin:20px 0}.tag{color:#145d65;font-size:12px;font-weight:800;letter-spacing:1px}table{border-collapse:collapse;width:100%}th,td{padding:12px;text-align:left;border-bottom:1px solid #ddd;vertical-align:top}a{color:#12616d}.notice{background:#fff0c5;padding:18px;border-radius:10px}@media(max-width:600px){th,td{padding:6px;font-size:13px}}</style><main><p class="tag">OFFERWATCH / OFFLINE PROTOTYPE</p><h1>What changed. What needs checking.</h1><p class="notice">'+('SYNTHETIC DEMONSTRATION — fictional companies and events. Not live market intelligence.' if report['synthetic'] else 'Reviewed snapshot comparison; not a live crawl or proof of commercial availability.')+'</p><p>Report timestamp: '+esc(report['as_of'])+'</p>'+''.join(cards)+'<p>Stable event IDs support later duplicate prevention. This prototype does not store delivery state, send alerts, collect pages, or run on a schedule. Missing information is not proof an offer ended.</p></main></html>'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('before')
    parser.add_argument('after')
    parser.add_argument('output')
    args = parser.parse_args()
    report = compare(json.loads(Path(args.before).read_text()), json.loads(Path(args.after).read_text()))
    Path(args.output).write_text(render(report), encoding='utf-8')
    Path(args.output).with_suffix('.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
