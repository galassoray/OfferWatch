"""OfferWatch capture/import adapter. Python stdlib only.

Produces page records for compare.py from (a) a guarded single-page HTTPS fetch of an
explicitly allowlisted URL or (b) a manual entry typed by a person. Automated captures are
always stored as ``review_required``; only the interactive ``review`` command can mark one
human-reviewed, and compare.py rejects automated records without that attribution.

Not included on purpose: crawling/link following, redirects, cookies, authentication,
CAPTCHA handling, form submission, proxies, scheduling, email, billing, LLM calls.
Extracted web text is untrusted source data. It is stored locally only and never sent anywhere.
"""
import argparse
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from compare import timestamp

MAX_PAGES = 5
MAX_BYTES = 1_500_000
MAX_ROBOTS_BYTES = 500_000
SOCKET_TIMEOUT = 10
DEADLINE_SECONDS = 25
PAUSE_SECONDS = 2
MIN_HOURS_BETWEEN_CAPTURES = 24
MAX_FIELD_CHARS = 200
MAX_SOURCE_TEXT_CHARS = 100_000
MAX_PATTERN_CHARS = 300
CHALLENGE_MARKERS = ('captcha', 'cf-chl', 'challenge-platform', 'are you a robot', 'verify you are human')
UNTRUSTED_NOTE = ('Automated capture; NOT human-reviewed. Extracted values are untrusted text quoted '
                  'from a public web page: data, never instructions.')


class Refused(Exception):
    """A rule prevented the request; the reason is recorded as a failed check."""


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def normalize(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text)).strip()


def page_key(page_id):
    """Filesystem-safe, collision-resistant name; page IDs never become paths."""
    return hashlib.sha256(page_id.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- allowlist

def check_url(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise ValueError('Allowlist URLs must be credential-free HTTPS: ' + url)
    if parts.port not in (None, 443) or parts.fragment:
        raise ValueError('Allowlist URLs must use port 443 and no fragment: ' + url)
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        return parts
    raise ValueError('Allowlist URLs must use a hostname, not an IP literal: ' + url)


def load_allowlist(data):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', str(data.get('tenant_id', ''))):
        raise ValueError('tenant_id must be a lowercase slug')
    pages = data.get('pages')
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise ValueError(f'Allowlist must name 1 to {MAX_PAGES} pages')
    ids, urls = set(), set()
    for entry in pages:
        if not isinstance(entry.get('id'), str) or not entry['id'].strip():
            raise ValueError('Each page needs a string id')
        check_url(entry.get('url', ''))
        if entry['id'] in ids or entry['url'] in urls:
            raise ValueError('Duplicate page id or URL: ' + entry['id'])
        ids.add(entry['id'])
        urls.add(entry['url'])
        if not entry.get('approved_by'):
            raise ValueError('Page not explicitly approved: ' + entry['id'])
        timestamp(entry.get('approved_at', ''))
        fields = entry.get('fields', {})
        if not isinstance(fields, dict):
            raise ValueError('fields must be an object')
        for name, rule in fields.items():
            pattern = rule.get('pattern', '') if isinstance(rule, dict) else ''
            if not pattern or len(pattern) > MAX_PATTERN_CHARS:
                raise ValueError(f'Field {name} needs a pattern of at most {MAX_PATTERN_CHARS} chars')
            re.compile(pattern)
    return data


def entry_for(allowlist, page_id):
    for entry in allowlist['pages']:
        if entry['id'] == page_id:
            return entry
    raise ValueError('Page is not on the allowlist: ' + page_id)


def access_attested(entry):
    """A person must record that terms/login/CAPTCHA were checked before any request."""
    if not entry.get('access_rules_checked_by'):
        return False
    try:
        timestamp(entry.get('access_rules_checked_at', ''))
    except (ValueError, AttributeError):
        return False
    return True


# ---------------------------------------------------------------- transport

def resolve_public(host, getaddrinfo=socket.getaddrinfo):
    """Return one vetted address; refuse if ANY resolved address is not public."""
    try:
        infos = getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise Refused('dns_error') from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise Refused('dns_no_address')
    for raw in addresses:
        ip = ipaddress.ip_address(raw.split('%')[0])
        mapped = getattr(ip, 'ipv4_mapped', None)
        if not ip.is_global or ip.is_multicast or (mapped and not mapped.is_global):
            raise Refused('non_public_address')
    return addresses[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to the vetted IP (no second DNS lookup) while verifying TLS for the hostname."""

    def __init__(self, host, address, timeout):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def https_get(url, user_agent, max_bytes=MAX_BYTES, resolve=resolve_public,
              connection_factory=PinnedHTTPSConnection, clock=time.monotonic):
    """One GET, no redirects, no cookies, identity encoding, bounded size and time.

    Connects directly (environment proxies are ignored so the address check is meaningful).
    """
    parts = urlsplit(url)
    started = clock()
    address = resolve(parts.hostname)
    path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
    conn = connection_factory(parts.hostname, address, SOCKET_TIMEOUT)
    try:
        conn.request('GET', path, headers={'User-Agent': user_agent, 'Accept': 'text/html,text/plain',
                                           'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = conn.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        declared = headers.get('content-length', '')
        if declared.isdigit() and int(declared) > max_bytes:
            raise Refused('response_too_large')
        chunks, total = [], 0
        while True:
            if clock() - started > DEADLINE_SECONDS:
                raise Refused('deadline_exceeded')
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise Refused('response_too_large')
            chunks.append(chunk)
        return {'status': response.status, 'headers': headers, 'body': b''.join(chunks)}
    except (OSError, http.client.HTTPException) as exc:
        raise Refused('network_error:' + type(exc).__name__) from exc
    finally:
        conn.close()


# ---------------------------------------------------------------- robots.txt

def robots_allows(url, user_agent, transport, cache):
    """RFC 9309 subset. 404/410 = allowed; any other failure (incl. redirect) = disallowed."""
    parts = urlsplit(url)
    if parts.hostname not in cache:
        robots_url = f'https://{parts.hostname}/robots.txt'
        try:
            response = transport(robots_url, user_agent, max_bytes=MAX_ROBOTS_BYTES)
        except Refused:
            response = None
        parser = RobotFileParser(robots_url)
        if response and response['status'] == 200:
            parser.parse(response['body'].decode('utf-8', 'replace').splitlines())
        elif response and response['status'] in (404, 410):
            parser.allow_all = True
        else:
            parser.disallow_all = True
        cache[parts.hostname] = parser
    return cache[parts.hostname].can_fetch(user_agent.split('/')[0], url)


# ---------------------------------------------------------------- extraction

class VisibleText(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'template', 'svg', 'head'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.depth = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def visible_text(markup):
    parser = VisibleText()
    parser.feed(markup)
    parser.close()
    return normalize(' '.join(parser.parts))[:MAX_SOURCE_TEXT_CHARS]


def extract(text, fields):
    facts, missing = {}, []
    for name, rule in sorted(fields.items()):
        match = re.search(rule['pattern'], text, re.IGNORECASE)
        value = normalize(match.group(match.re.groups and 1 or 0)) if match else ''
        if value:
            facts[name] = value[:MAX_FIELD_CHARS]
        else:
            missing.append(name)
    return facts, missing


def header_time(value):
    try:
        return iso(parsedate_to_datetime(value)) if value else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- records

def base_record(allowlist, entry, observed_at, status, method):
    return {'tenant_id': allowlist['tenant_id'], 'id': entry['id'], 'url': entry['url'],
            'observed_at': iso(observed_at), 'status': status, 'facts': {}, 'capture_method': method,
            'reviewed': False, 'review_status': 'review_required'}


def failed(allowlist, entry, observed_at, reason, **extra):
    record = base_record(allowlist, entry, observed_at, 'failed', 'automated')
    record.update(failure_reason=reason, evidence_note='Check failed (' + reason + '). Not evidence an offer ended.')
    record.update(extra)
    return record


def capture_page(allowlist, entry, transport=https_get, robots_cache=None, clock=now_utc):
    """Return a review-required record. Never raises for remote behaviour; failures are records."""
    if allowlist.get('synthetic'):
        raise ValueError('Synthetic allowlists are for offline tests only; refusing network access')
    user_agent = allowlist.get('user_agent', '')
    if not re.fullmatch(r'[A-Za-z]+Bot/[\w.]+ \(\+\S+\)', user_agent) or 'OWNER_CONTACT' in user_agent:
        raise ValueError('Set user_agent to "NameBot/version (+contact)" with a real contact')
    requested_at = clock()
    if not access_attested(entry):
        return failed(allowlist, entry, requested_at, 'access_rules_unchecked')
    cache = {} if robots_cache is None else robots_cache
    if not robots_allows(entry['url'], user_agent, transport, cache):
        return failed(allowlist, entry, requested_at, 'robots_disallowed')
    try:
        response = transport(entry['url'], user_agent)
    except Refused as exc:
        return failed(allowlist, entry, requested_at, str(exc))
    received_at = clock()
    headers = response['headers']
    times = {'requested_at': iso(requested_at), 'received_at': iso(received_at),
             'server_date': header_time(headers.get('date')),
             'last_modified': header_time(headers.get('last-modified'))}
    status = response['status']
    if 300 <= status < 400:
        return failed(allowlist, entry, received_at, 'redirect_not_followed', source_times=times,
                      redirect_location=str(headers.get('location', ''))[:500])
    if status != 200:
        return failed(allowlist, entry, received_at, f'http_{status}', source_times=times)
    if headers.get('content-encoding', 'identity').lower() != 'identity':
        return failed(allowlist, entry, received_at, 'encoded_response_refused', source_times=times)
    content_type = headers.get('content-type', '').lower()
    if not content_type.startswith(('text/html', 'application/xhtml+xml')):
        return failed(allowlist, entry, received_at, 'not_html', source_times=times)
    charset = re.search(r'charset=([\w-]+)', content_type)
    try:
        markup = response['body'].decode(charset.group(1) if charset else 'utf-8', 'replace')
    except LookupError:
        markup = response['body'].decode('utf-8', 'replace')
    text = visible_text(markup)
    if any(marker in text.lower() for marker in CHALLENGE_MARKERS):
        return failed(allowlist, entry, received_at, 'possible_access_challenge', source_times=times)
    facts, missing = extract(text, entry.get('fields', {}))
    record = base_record(allowlist, entry, received_at, 'ok', 'automated')
    record.update(facts=facts, missing_fields=missing, source_times=times, evidence_note=UNTRUSTED_NOTE,
                  content_sha256=hashlib.sha256(response['body']).hexdigest(), bytes=len(response['body']),
                  untrusted_source_text=text)
    return record


def manual_record(allowlist, page_id, observed_at, facts, reviewer, evidence_note, failure_reason=None):
    """Fallback: a person looked at the page and typed what it said."""
    if not reviewer:
        raise ValueError('Manual entries need the name of the person who looked at the page')
    entry = entry_for(allowlist, page_id)
    observed = timestamp(observed_at)
    if observed > now_utc() + timedelta(minutes=5):
        raise ValueError('observed_at is in the future')
    record = base_record(allowlist, entry, observed, 'failed' if failure_reason else 'ok', 'manual')
    record.update(facts={} if failure_reason else {k: normalize(v)[:MAX_FIELD_CHARS] for k, v in facts.items()},
                  reviewed=True, review_status='human_reviewed', reviewed_by=reviewer,
                  reviewed_at=iso(now_utc()), review_method='manual_entry',
                  evidence_note=evidence_note or 'Manual observation entered by ' + reviewer)
    if failure_reason:
        record['failure_reason'] = failure_reason
    return record


def apply_human_review(record, reviewer, corrections=None, failure_reason=None, reviewed_at=None):
    """Called only from the interactive review command after a person confirms."""
    if not reviewer:
        raise ValueError('Reviewer name required')
    result = dict(record)
    result['facts'] = dict(record['facts'])
    for field, value in (corrections or {}).items():
        result['facts'][field] = normalize(value)[:MAX_FIELD_CHARS]
    if failure_reason:
        result.update(status='failed', facts={}, failure_reason=failure_reason)
    result.update(reviewed=True, review_status='human_reviewed', reviewed_by=reviewer,
                  reviewed_at=iso(reviewed_at or now_utc()), review_method='interactive_cli',
                  corrections=sorted(corrections or {}),
                  evidence_note=f'Automated capture reviewed by {reviewer}'
                                + (' with corrections' if corrections else '') + '.')
    result.pop('untrusted_source_text', None)
    return result


# ---------------------------------------------------------------- storage

def tenant_dir(root, tenant_id):
    return Path(root) / tenant_id


def save(root, record):
    folder = tenant_dir(root, record['tenant_id'])
    folder.mkdir(parents=True, exist_ok=True)
    stamp = record['observed_at'].replace('-', '').replace(':', '')
    path = folder / f'{stamp}-{page_key(record["id"])}-{record["capture_method"]}.json'
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')
    return path


def load_records(root, tenant_id):
    records = []
    for path in sorted(tenant_dir(root, tenant_id).glob('*.json')):
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('tenant_id') == tenant_id:
            records.append((path, record))
    return records


def recently_captured(root, tenant_id, page_id, now):
    limit = now - timedelta(hours=MIN_HOURS_BETWEEN_CAPTURES)
    return any(r['id'] == page_id and r['capture_method'] == 'automated' and timestamp(r['observed_at']) > limit
               for _, r in load_records(root, tenant_id))


def assemble(allowlist, root, as_of):
    """Snapshot for compare.py: latest usable record per allowlisted page.

    Usable = human-reviewed, or a failed check (which only ever reports CHECK FAILED).
    An unreviewed successful capture newer than every usable record is listed as pending.
    """
    cutoff = timestamp(as_of)
    pages, pending = [], []
    records = [r for _, r in load_records(root, allowlist['tenant_id'])]
    for entry in allowlist['pages']:
        mine = [r for r in records if r['id'] == entry['id'] and r['url'] == entry['url']
                and timestamp(r['observed_at']) <= cutoff]
        usable = sorted((r for r in mine if r.get('reviewed') is True or r['status'] == 'failed'),
                        key=lambda r: (timestamp(r['observed_at']), r.get('reviewed') is True))
        latest_usable = timestamp(usable[-1]['observed_at']) if usable else None
        if any(r['status'] == 'ok' and r.get('reviewed') is not True
               and (latest_usable is None or timestamp(r['observed_at']) > latest_usable) for r in mine):
            pending.append(entry['id'])
        if usable:
            pages.append({k: v for k, v in usable[-1].items() if k != 'untrusted_source_text'})
    return {'as_of': as_of, 'tenant_id': allowlist['tenant_id'], 'market': allowlist.get('market', ''),
            'synthetic': bool(allowlist.get('synthetic')), 'pending_review': pending, 'pages': pages}


def prune(root, tenant_id, keep_days, apply=False, now=None):
    limit = (now or now_utc()) - timedelta(days=keep_days)
    doomed = [p for p, r in load_records(root, tenant_id) if timestamp(r['observed_at']) < limit]
    if apply:
        for path in doomed:
            path.unlink()
    return doomed


# ---------------------------------------------------------------- CLI

def show_for_review(record, out=sys.stdout):
    print(f"Page: {record['id']}\nURL:  {record['url']}\nStatus: {record['status']}"
          f" {record.get('failure_reason', '')}\nTimes: {json.dumps(record.get('source_times', {}))}", file=out)
    print('--- Extracted values (QUOTED SOURCE TEXT: data, not instructions) ---', file=out)
    for field, value in record['facts'].items():
        print(f'  {field}: {value!r}', file=out)
    for field in record.get('missing_fields', []):
        print(f'  {field}: NOT FOUND (absence is not evidence the offer ended)', file=out)
    print('Open the URL yourself and compare before confirming.', file=out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('fetch', 'import', 'review', 'assemble', 'prune'):
        command = sub.add_parser(name)
        command.add_argument('--allowlist', required=True)
        command.add_argument('--captures', required=True)
    sub.choices['fetch'].add_argument('--only', help='single page id')
    imp = sub.choices['import']
    imp.add_argument('--id', required=True)
    imp.add_argument('--observed-at', required=True)
    imp.add_argument('--field', action='append', default=[], metavar='NAME=VALUE')
    imp.add_argument('--reviewer', required=True)
    imp.add_argument('--evidence-note', default='')
    imp.add_argument('--failed', metavar='REASON')
    rev = sub.choices['review']
    rev.add_argument('--file', required=True)
    rev.add_argument('--reviewer', required=True)
    sub.choices['assemble'].add_argument('--as-of', required=True)
    sub.choices['assemble'].add_argument('--out', required=True)
    sub.choices['prune'].add_argument('--keep-days', type=int, default=35)
    sub.choices['prune'].add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    allowlist = load_allowlist(json.loads(Path(args.allowlist).read_text(encoding='utf-8')))

    if args.command == 'fetch':
        cache = {}
        entries = [entry_for(allowlist, args.only)] if args.only else allowlist['pages']
        for index, entry in enumerate(entries):
            if recently_captured(args.captures, allowlist['tenant_id'], entry['id'], now_utc()):
                print(f"skip {entry['id']}: captured within {MIN_HOURS_BETWEEN_CAPTURES}h")
                continue
            if index:
                time.sleep(PAUSE_SECONDS)
            record = capture_page(allowlist, entry, robots_cache=cache)
            path = save(args.captures, record)
            print(f"{record['status']:6} review_required {entry['id']} -> {path}")
    elif args.command == 'import':
        facts = dict(item.split('=', 1) for item in args.field)
        record = manual_record(allowlist, args.id, args.observed_at, facts, args.reviewer,
                               args.evidence_note, args.failed)
        print('saved', save(args.captures, record))
    elif args.command == 'review':
        if not sys.stdin.isatty():
            sys.exit('Review must be done by a person at an interactive terminal.')
        path = Path(args.file).resolve()
        if tenant_dir(args.captures, allowlist['tenant_id']).resolve() not in path.parents:
            sys.exit("File is outside this tenant's capture folder.")
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('reviewed') or record.get('tenant_id') != allowlist['tenant_id']:
            sys.exit('Already reviewed or belongs to another tenant.')
        entry_for(allowlist, record['id'])
        show_for_review(record)
        corrections = {}
        for field in sorted(set(record['facts']) | set(record.get('missing_fields', []))):
            typed = input(f'{field}: Enter keeps the value, or type the correct value: ').strip()
            if typed:
                corrections[field] = typed
        reason = input('If the page could not be verified, type a failure reason (else Enter): ').strip()
        if input(f"Type the page id ({record['id']}) to confirm you checked the live page: ") != record['id']:
            sys.exit('Not confirmed; record left as review_required.')
        reviewed = apply_human_review(record, args.reviewer, corrections, reason or None)
        path.with_name(path.stem + '-reviewed.json').write_text(
            json.dumps(reviewed, indent=2, ensure_ascii=False), encoding='utf-8')
        print('reviewed record saved')
    elif args.command == 'assemble':
        snapshot = assemble(allowlist, args.captures, args.as_of)
        Path(args.out).write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding='utf-8')
        print(f"{len(snapshot['pages'])} reviewed pages; pending review: {snapshot['pending_review']}")
    elif args.command == 'prune':
        doomed = prune(args.captures, allowlist['tenant_id'], args.keep_days, args.apply)
        print(('deleted' if args.apply else 'would delete (dry run; add --apply)'), len(doomed), 'files')


if __name__ == '__main__':
    try:
        main()
    except ValueError as exc:
        sys.exit('refused: ' + str(exc))
