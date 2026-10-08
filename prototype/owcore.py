"""Shared helpers: tenant identifiers, timestamps, conservative normalisation, safe links."""
import hashlib
import html
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit

TENANT_RE = re.compile(r'[a-z0-9][a-z0-9-]{1,62}')

# Presentation-only differences. Everything else (digits, currency, %, dates, words,
# footnote marks, superscripts, fractions, em dashes) is preserved exactly.
_SPACE_CHARS = '\t\n\r\f\v               　'
_DROP_CHARS = '​‌‍⁠﻿­'
_PRESENTATION = {ord(c): ' ' for c in _SPACE_CHARS}
_PRESENTATION.update({ord(c): None for c in _DROP_CHARS})
_PRESENTATION.update({0x2010: '-', 0x2011: '-', 0x2012: '-', 0x2013: '-',
                      0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"'})
_PRESENTATION.update({code: chr(code - 0xFEE0) for code in range(0xFF01, 0xFF5F)})  # full-width ASCII


def require_tenant(value, what='tenant_id'):
    if not isinstance(value, str) or not TENANT_RE.fullmatch(value):
        raise ValueError(f'{what} must be a lowercase slug of 2-63 characters')
    return value


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be a string')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return result


def iso(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def age_hours(earlier, later):
    return round((timestamp(later) - timestamp(earlier)).total_seconds() / 3600, 1)


def normalize(text):
    """Conservative: whitespace, invisible characters, typographic quotes/hyphens, full-width ASCII."""
    text = unicodedata.normalize('NFC', str(text)).translate(_PRESENTATION)
    return re.sub(r' +', ' ', text).strip()


def compare_key(field, value, rules=None):
    """The value used to decide whether a field changed. Case folding only where configured."""
    value = normalize(value)
    rule = (rules or {}).get(field, {})
    return value.casefold() if rule.get('case') == 'insensitive' else value


def safe_href(url):
    """Return the URL only if it is a credential-free https URL without control characters."""
    if not isinstance(url, str) or re.search(r'[\x00-\x20\x7f"<>\\`]', url):
        return None
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        return None
    return url


def esc(value):
    return html.escape(str(value), quote=True)


def digest(*parts, length=20):
    canonical = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:length]
