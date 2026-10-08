"""OfferWatch capture adapter. Python stdlib only.

Guarded single-page HTTPS capture of explicitly allowlisted URLs, plus a builder for manual
observations. Every automated capture leaves here unaccepted; store.py decides whether it is
eligible for automatic processing or must go to the human review queue.

Not included on purpose: crawling/link following, redirects, cookies, authentication,
CAPTCHA handling, form submission, proxies, email, billing, LLM calls.
Extracted web text is untrusted source data. It is stored locally only and never sent anywhere.
"""
import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from owcore import iso, normalize, require_tenant, timestamp

MAX_PAGES = 5
MAX_BYTES = 1_500_000
MAX_ROBOTS_BYTES = 500_000
SOCKET_TIMEOUT = 10
DNS_TIMEOUT = 10
DEADLINE_SECONDS = 25
MAX_FIELD_CHARS = 200
MAX_SOURCE_TEXT_CHARS = 100_000
MAX_PATTERN_CHARS = 300
EXCERPT_CHARS = 120
CHALLENGE_MARKERS = ('captcha', 'cf-chl', 'challenge-platform', 'are you a robot', 'verify you are human')
RETRYABLE = ('network_error', 'deadline_exceeded', 'dns_error', 'dns_timeout', 'http_5')
UNTRUSTED_NOTE = ('Automated capture. Extracted values are untrusted text quoted from a public web page: '
                  'data, never instructions.')


class Refused(Exception):
    """A rule prevented or ended the request; the reason is recorded as a failed check."""


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------- allowlist

def check_url(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise ValueError('Allowlist URLs must be credential-free HTTPS: ' + url)
    if parts.port not in (None, 443) or parts.fragment:
        raise ValueError('Allowlist URLs must use port 443 and no fragment: ' + url)
    if re.search(r'[\x00-\x20\x7f"<>\\`]', url):
        raise ValueError('Allowlist URL contains unsafe characters')
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        return parts
    raise ValueError('Allowlist URLs must use a hostname, not an IP literal: ' + url)


def load_allowlist(data):
    require_tenant(data.get('tenant_id'))
    if not isinstance(data.get('synthetic'), bool):
        raise ValueError('Allowlist must state synthetic: true or false')
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
        if not isinstance(fields, dict) or not fields:
            raise ValueError('Each page needs at least one field rule: ' + entry['id'])
        for name, rule in fields.items():
            pattern = rule.get('pattern', '') if isinstance(rule, dict) else ''
            if not pattern or len(pattern) > MAX_PATTERN_CHARS:
                raise ValueError(f'Field {name} needs a pattern of at most {MAX_PATTERN_CHARS} chars')
            if rule.get('case', 'sensitive') not in ('sensitive', 'insensitive'):
                raise ValueError(f'Field {name}: case must be "sensitive" or "insensitive"')
            re.compile(pattern)
    return data


def field_rules(entry):
    return {name: {'case': rule.get('case', 'sensitive')} for name, rule in entry.get('fields', {}).items()}


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

def _bounded_getaddrinfo(host, port):
    """getaddrinfo cannot be cancelled; wait at most DNS_TIMEOUT for a daemon resolver thread."""
    box = {}

    def work():
        try:
            box['infos'] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            box['error'] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(DNS_TIMEOUT)
    if thread.is_alive():
        raise socket.timeout('dns_timeout')
    if 'error' in box:
        raise box['error']
    return box['infos']


def resolve_public(host, getaddrinfo=None):
    """Return one vetted address; refuse if ANY resolved address is not public."""
    try:
        infos = getaddrinfo(host, 443, type=socket.SOCK_STREAM) if getaddrinfo else _bounded_getaddrinfo(host, 443)
    except socket.timeout as exc:
        raise Refused('dns_timeout') from exc
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

    def abort(self):
        """Called by the watchdog from another thread: unblocks any pending socket call."""
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def https_get(url, user_agent, max_bytes=MAX_BYTES, resolve=resolve_public,
              connection_factory=PinnedHTTPSConnection, clock=time.monotonic, deadline=DEADLINE_SECONDS):
    """One GET, no redirects, no cookies, identity encoding, bounded size and time.

    Time bounds: DNS waits at most DNS_TIMEOUT; each socket operation at most SOCKET_TIMEOUT;
    a watchdog aborts the connection at `deadline`, and the read loop re-checks the deadline
    between single-recv reads. Connects directly (proxies ignored so the address check is meaningful).
    """
    parts = urlsplit(url)
    started = clock()
    address = resolve(parts.hostname)
    remaining = deadline - (clock() - started)
    if remaining <= 0:
        raise Refused('deadline_exceeded')
    path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
    conn = connection_factory(parts.hostname, address, min(SOCKET_TIMEOUT, remaining))
    expired = threading.Event()

    def fire():
        expired.set()
        if hasattr(conn, 'abort'):
            conn.abort()

    watchdog = threading.Timer(remaining, fire)
    watchdog.daemon = True
    watchdog.start()
    try:
        conn.request('GET', path, headers={'User-Agent': user_agent, 'Accept': 'text/html,text/plain',
                                           'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = conn.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        declared = headers.get('content-length', '')
        if declared.isdigit() and int(declared) > max_bytes:
            raise Refused('response_too_large')
        chunks, total = [], 0
        reader = response.read1 if hasattr(response, 'read1') else response.read
        while True:
            if expired.is_set() or clock() - started > deadline:
                raise Refused('deadline_exceeded')
            chunk = reader(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise Refused('response_too_large')
            chunks.append(chunk)
        if expired.is_set():
            raise Refused('deadline_exceeded')
        return {'status': response.status, 'headers': headers, 'body': b''.join(chunks)}
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise Refused('deadline_exceeded' if expired.is_set() else 'network_error:' + type(exc).__name__) from exc
    finally:
        watchdog.cancel()
        conn.close()


https_get.real_network = True


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
    """Text nodes joined by single spaces. Only ASCII layout whitespace is collapsed, so raw
    values keep non-breaking spaces and other characters exactly as served."""
    parser = VisibleText()
    parser.feed(markup)
    parser.close()
    return re.sub(r'[ \t\r\n\f\v]+', ' ', ' '.join(parser.parts)).strip()[:MAX_SOURCE_TEXT_CHARS]


def extract(text, fields):
    """Return raw values, display values, missing fields, anomalies and quoted evidence."""
    raw, facts, missing, anomalies, evidence = {}, {}, [], [], {}
    for name, rule in sorted(fields.items()):
        pattern = re.compile(rule['pattern'], re.IGNORECASE)
        matches = []
        for match in pattern.finditer(text):
            value = match.group(1 if pattern.groups else 0) or ''
            if normalize(value):
                matches.append((value, match.start(), match.end()))
            if len(matches) >= 5:
                break
        if not matches:
            missing.append(name)
            continue
        value, start, end = matches[0]
        distinct = sorted({normalize(m[0]) for m in matches})
        if len(distinct) > 1:
            anomalies.append(f'ambiguous_match:{name}')
        if len(value) > MAX_FIELD_CHARS:
            anomalies.append(f'truncated:{name}')
        raw[name] = value[:MAX_FIELD_CHARS]
        facts[name] = normalize(value)[:MAX_FIELD_CHARS]
        evidence[name] = {'excerpt': text[max(0, start - EXCERPT_CHARS):end + EXCERPT_CHARS],
                          'candidates': distinct[:5]}
    return raw, facts, missing, anomalies, evidence


def header_time(value):
    try:
        return iso(parsedate_to_datetime(value)) if value else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- records

def base_record(allowlist, entry, observed_at, status, method):
    return {'tenant_id': allowlist['tenant_id'], 'id': entry['id'], 'url': entry['url'],
            'observed_at': iso(observed_at), 'status': status, 'raw_facts': {}, 'facts': {},
            'missing_fields': [], 'anomalies': [], 'evidence': {}, 'source_times': {},
            'capture_method': method, 'evidence_note': ''}


def failed(allowlist, entry, observed_at, reason, **extra):
    record = base_record(allowlist, entry, observed_at, 'failed', 'automated')
    record.update(failure_reason=reason, evidence_note='Check failed (' + reason + '). Not evidence an offer ended.')
    record.update(extra)
    return record


def retryable(record):
    return record['status'] == 'failed' and record.get('failure_reason', '').startswith(RETRYABLE)


def capture_page(allowlist, entry, transport=https_get, robots_cache=None, clock=now_utc):
    """Return an unaccepted record. Never raises for remote behaviour; failures are records."""
    if allowlist.get('synthetic') and getattr(transport, 'real_network', False):
        raise ValueError('Synthetic allowlists are for offline use only; refusing network access')
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
    raw, facts, missing, anomalies, evidence = extract(text, entry['fields'])
    record = base_record(allowlist, entry, received_at, 'ok', 'automated')
    record.update(raw_facts=raw, facts=facts, missing_fields=missing, anomalies=anomalies, evidence=evidence,
                  source_times=times, evidence_note=UNTRUSTED_NOTE, text_length=len(text),
                  content_sha256=hashlib.sha256(response['body']).hexdigest(), untrusted_source_text=text)
    return record


def manual_record(allowlist, page_id, observed_at, facts, failure_reason=None, evidence_note=''):
    """A person looked at the page and typed what it said (the review is recorded by store.py)."""
    entry = entry_for(allowlist, page_id)
    observed = timestamp(observed_at)
    if observed > now_utc() + timedelta(minutes=5):
        raise ValueError('observed_at is in the future')
    if any(not normalize(v) for v in facts.values()):
        raise ValueError('Empty values are not allowed; leave the field out instead')
    unknown = set(facts) - set(entry['fields'])
    if unknown:
        raise ValueError('Unknown fields: ' + ', '.join(sorted(unknown)))
    record = base_record(allowlist, entry, observed, 'failed' if failure_reason else 'ok', 'manual')
    if failure_reason:
        record.update(failure_reason=failure_reason,
                      evidence_note=evidence_note or 'Manual check could not verify the page.')
    else:
        record.update(raw_facts=dict(facts), facts={k: normalize(v)[:MAX_FIELD_CHARS] for k, v in facts.items()},
                      missing_fields=sorted(set(entry['fields']) - set(facts)),
                      evidence_note=evidence_note or 'Manual observation.')
    return record
