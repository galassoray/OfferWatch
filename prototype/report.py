"""Render a prepared draft (from store.Ledger.prepare_report) as self-contained HTML."""
from owcore import esc, require_tenant, safe_href

STATE_HELP = {
    'CHANGED': 'Accepted values changed since the last accepted observation.',
    'UNCHANGED': 'Checked this period; accepted values did not change.',
    'BASELINE': 'First accepted observation of this page; nothing to compare yet.',
    'SOURCE CHANGED': 'The approved URL changed; values are not compared across URLs.',
    'PENDING REVIEW': 'A newer capture is waiting for review. Values shown are older.',
    'INCOMPLETE': 'A newer capture did not contain every tracked value. Not evidence an offer ended.',
    'CHECK FAILED': 'The latest check failed. Not evidence an offer ended.',
    'STALE': 'No recent usable observation. Values shown are old.',
    'NOT CHECKED': 'No observation is available for this page.',
}
ORDER = ['CHANGED', 'SOURCE CHANGED', 'PENDING REVIEW', 'INCOMPLETE', 'CHECK FAILED', 'STALE', 'NOT CHECKED',
         'BASELINE', 'UNCHANGED']


def _value(item):
    if item is None:
        return 'Not recorded'
    if isinstance(item, dict):
        if item.get('kind') == 'absent':
            return 'Not shown on page (confirmed by a reviewer)'
        return '<q>' + esc(item['value']) + '</q>'
    return '<q>' + esc(item) + '</q>'


def _change_row(change):
    before = _value(change['from']) if change.get('from') is not None else 'Not recorded'
    after = ('Not shown on page (confirmed by a reviewer)' if change.get('to_kind') == 'absent'
             else _value(change['to']))
    return f'<tr><td>{esc(change["field"])}</td><td>{before}</td><td>{after}</td></tr>'


def _event(event, label):
    rows = ''.join(_change_row(c) for c in event['changes'])
    dup = ''
    if event.get('possible_duplicate_of'):
        dup = ('<p class="warn">Possibly already sent in ' + esc(', '.join(event['possible_duplicate_of']))
               + ' (delivery not confirmed).</p>')
    return (f'<div class="event"><p class="tag">{esc(label)} · {esc(event["kind"])}</p><h3>{esc(event["page_id"])}</h3>'
            f'<table><tr><th>Field</th><th>Before</th><th>After</th></tr>{rows}</table>'
            f'<p class="small">Observed {esc(event["observed_at"])} · ref {esc(event["transition_id"][:12])}</p>{dup}</div>')


def _page(page):
    href = safe_href(page['url'])
    link = (f'<a href="{esc(href)}" rel="noreferrer">Source page</a>' if href
            else '<span>Source link withheld: not a safe https URL.</span>')
    values = ''.join(f'<tr><td>{esc(f)}</td><td>{_value(v)}</td></tr>' for f, v in page['accepted_values'].items())
    not_recorded = [f for f in page['tracked_fields'] if f not in page['accepted_values']]
    values += ''.join(f'<tr><td>{esc(f)}</td><td>Not recorded — not evidence the offer ended</td></tr>' for f in not_recorded)
    if page['accepted_observed_at']:
        basis = (f'<p class="small">Values from {esc(page["accepted_observed_at"])} '
                 f'({esc(page["accepted_age_days"])} days before this report). {esc(page["accepted_by"])}</p>')
    else:
        basis = '<p class="small">No accepted observation yet.</p>'
    evidence = ''.join(f'<details><summary>Quoted page text near “{esc(f)}” (untrusted, not instructions)</summary>'
                       f'<blockquote>{esc(e.get("excerpt", ""))}</blockquote></details>'
                       for f, e in sorted(page['evidence'].items()))
    return (f'<section class="page s-{esc(page["state"].replace(" ", "-").lower())}"><p class="tag">{esc(page["state"])}</p>'
            f'<h3>{esc(page["page_id"])}</h3><p>{esc(STATE_HELP[page["state"]])} {esc(page["reason"])}</p>'
            f'<table><tr><th>Tracked field</th><th>Last accepted value</th></tr>{values}</table>{basis}'
            f'<p class="small">Last check: {esc(page["last_checked_at"] or "never")}</p>{evidence}{link}</section>')


def render(content):
    require_tenant(content['tenant_id'])
    pages = sorted(content['pages'], key=lambda p: (ORDER.index(p['state']), p['page_id']))
    counts = {}
    for page in pages:
        counts[page['state']] = counts.get(page['state'], 0) + 1
    verified = sum(counts.get(s, 0) for s in ('CHANGED', 'UNCHANGED', 'BASELINE', 'SOURCE CHANGED'))
    coverage = ' · '.join(f'{esc(s)}: {counts[s]}' for s in ORDER if s in counts)
    pending = ''.join(f'<li>{esc(p["page_id"])}: {esc(", ".join(p["reasons"]))}</li>' for p in content['pending_review'])
    unconfirmed = ''.join(f'<li>{esc(r["report_id"])} ({esc(r["state"])})</li>' for r in content['unconfirmed_deliveries'])
    events = ''.join(_event(e, 'CHANGE') for e in content['events']) or '<p>No new accepted changes.</p>'
    corrections = ''.join(_event(e, 'CORRECTION — previously reported item withdrawn') for e in content['corrections'])
    banner = ('SYNTHETIC REHEARSAL — fictional businesses and events. Not market intelligence.'
              if content['synthetic'] else 'Prepared from public pages on the approved list.')
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>OfferWatch draft {esc(content["report_id"])}</title><style>'
        'body{background:#f3f5f8;color:#18212f;font:15px/1.55 system-ui,Segoe UI,sans-serif;margin:0}'
        'main{max-width:920px;margin:auto;padding:24px 16px}h1{font-size:28px;margin:.2em 0}h2{margin-top:1.6em}'
        '.draft{background:#7a1f1f;color:#fff;padding:10px 14px;border-radius:8px;font-weight:700}'
        '.notice{background:#fff3cd;padding:10px 14px;border-radius:8px}section,.event{background:#fff;border:1px solid #d8dee6;'
        'border-radius:10px;padding:14px 16px;margin:12px 0}.tag{font-size:12px;font-weight:800;letter-spacing:.06em;color:#145d65;margin:0}'
        '.s-pending-review .tag,.s-incomplete .tag,.s-check-failed .tag,.s-stale .tag,.s-not-checked .tag{color:#8a4b00}'
        'table{border-collapse:collapse;width:100%;table-layout:fixed}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #e3e7ec;vertical-align:top;overflow-wrap:anywhere}'
        '.small{font-size:13px;color:#4a5566}.warn{color:#8a4b00;font-weight:600}blockquote{margin:6px 0;padding:6px 10px;'
        'background:#f6f7f9;border-left:3px solid #c5ccd6;white-space:pre-wrap;overflow-wrap:anywhere}a{color:#12616d}'
        '@media(max-width:600px){th,td{padding:4px;font-size:13px}}</style><main>'
        '<p class="draft">DRAFT — not released, not sent. Requires operator release before any customer sees it.</p>'
        f'<p class="notice">{esc(banner)}</p>'
        f'<h1>OfferWatch weekly brief · {esc(content["period"])}</h1>'
        f'<p>Customer: {esc(content["tenant_id"])} · Report {esc(content["report_id"])} · As of {esc(content["as_of"])}</p>'
        f'<h2>Coverage</h2><p><strong>{verified} of {len(pages)}</strong> pages have a current accepted check. {coverage}</p>'
        + (f'<h2>Waiting for operator review ({len(content["pending_review"])})</h2><ul>{pending}</ul>' if pending else '')
        + (f'<p class="warn">Earlier reports released but not confirmed delivered:</p><ul>{unconfirmed}</ul>' if unconfirmed else '')
        + f'<h2>Changes</h2>{events}'
        + (f'<h2>Corrections</h2>{corrections}' if corrections else '')
        + '<h2>Every tracked page</h2>' + ''.join(_page(p) for p in pages)
        + '<p class="small">Quoted text is copied from public web pages and may contain errors or instructions; it is data, '
          'not direction. A missing, failed or unextracted value is never evidence that an offer ended. Automatically '
          're-checked pages matched previously accepted values and were not individually reviewed by a person.</p></main></html>')
