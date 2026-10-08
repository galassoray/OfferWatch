"""Per-tenant transactional ledger (SQLite, stdlib).

One database file per tenant, and every row also carries tenant_id; both are checked on
every write. This is application-level separation inside one operating-system account. It
is not a security boundary: anything running as Ray can read every tenant's files.

Identities, kept distinct:
  tenant_id      who the data belongs to
  obs_id         one observation (hash of what was seen; notes are not part of it)
  transition_id  one meaningful change in accepted values (derived by replaying accepted
                 observations in time order, so reprocessing is idempotent)
  report_id      one prepared draft (prepared != released != delivered)
  delivery       acknowledged only by a person; nothing is ever marked sent automatically
"""
import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

from owcore import age_hours, compare_key, digest, iso, normalize, require_tenant, timestamp

SCHEMA_VERSION = '1'
AUTO_RULE = 'unchanged-complete-v1'
REVERIFY_DAYS = 56
MAX_CAPTURE_AGE_HOURS = 96
REPORT_WINDOW_DAYS = 7
TEXT_SHIFT_RANGE = (0.5, 2.0)
HUMAN_METHODS = ('interactive_cli', 'manual_entry')
APPROVED = ('human_accepted', 'fixture_accepted')
ACCEPTED = ('auto_accepted',) + APPROVED
DEFAULT_RETENTION = {'raw_text_days': 35, 'draft_report_days': 90}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
  tenant_id TEXT NOT NULL, page_id TEXT NOT NULL, url TEXT NOT NULL, approved_by TEXT NOT NULL,
  approved_at TEXT NOT NULL, rules TEXT NOT NULL, access_attested INTEGER NOT NULL, active INTEGER NOT NULL,
  PRIMARY KEY(tenant_id, page_id));
CREATE TABLE IF NOT EXISTS observations(
  obs_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, page_id TEXT NOT NULL, url TEXT NOT NULL,
  observed_at TEXT NOT NULL, capture_method TEXT NOT NULL CHECK(capture_method IN ('automated','manual')),
  check_status TEXT NOT NULL CHECK(check_status IN ('ok','failed')), failure_reason TEXT,
  raw_facts TEXT NOT NULL, facts TEXT NOT NULL, missing_fields TEXT NOT NULL, anomalies TEXT NOT NULL,
  evidence TEXT NOT NULL, source_times TEXT NOT NULL, content_sha256 TEXT, text_length INTEGER,
  machine_status TEXT NOT NULL, machine_reasons TEXT NOT NULL,
  acceptance TEXT NOT NULL CHECK(acceptance IN
    ('pending_review','auto_accepted','human_accepted','fixture_accepted','rejected','superseded','not_applicable')),
  accepted_rule TEXT, run_id TEXT, ingested_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', raw_text_path TEXT,
  FOREIGN KEY(tenant_id, page_id) REFERENCES sources(tenant_id, page_id));
CREATE INDEX IF NOT EXISTS obs_page ON observations(tenant_id, page_id, observed_at);
CREATE TABLE IF NOT EXISTS reviews(
  review_id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, obs_id TEXT NOT NULL REFERENCES observations(obs_id),
  reviewer TEXT NOT NULL, method TEXT NOT NULL CHECK(method IN ('interactive_cli','manual_entry','synthetic_fixture')),
  reviewed_at TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('accept','reject','correct')),
  corrections TEXT NOT NULL, confirmed_absent TEXT NOT NULL, comment TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transitions(
  transition_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, page_id TEXT NOT NULL, kind TEXT NOT NULL,
  from_obs_id TEXT, to_obs_id TEXT NOT NULL, observed_at TEXT NOT NULL, changes TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','retracted')), created_at TEXT NOT NULL, retracted_at TEXT);
CREATE TABLE IF NOT EXISTS reports(
  report_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, period TEXT NOT NULL, revision INTEGER NOT NULL,
  as_of TEXT NOT NULL, prepared_at TEXT NOT NULL, content_sha256 TEXT NOT NULL, html_path TEXT, json_path TEXT,
  state TEXT NOT NULL CHECK(state IN ('draft','released','delivered_ack','delivery_unknown','void')),
  released_by TEXT, released_at TEXT, release_method TEXT,
  delivery_ack_by TEXT, delivery_ack_at TEXT, delivery_channel TEXT, delivery_note TEXT, files_pruned INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS report_items(
  report_id TEXT NOT NULL REFERENCES reports(report_id), transition_id TEXT NOT NULL REFERENCES transitions(transition_id),
  role TEXT NOT NULL CHECK(role IN ('event','retraction')), PRIMARY KEY(report_id, transition_id, role));
"""

_TOKEN = object()


class ReviewSession:
    """Who is approving. Only interactive_session() or synthetic_session() can create one.

    This prevents accidental automation from recording approvals; it does not stop
    deliberately malicious code running under the same account.
    """

    def __init__(self, reviewer, method, token):
        if token is not _TOKEN:
            raise PermissionError('Use interactive_session() or synthetic_session()')
        self.reviewer, self.method = reviewer, method


def interactive_session(reviewer, method='interactive_cli', stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if not (stdin.isatty() and stdout.isatty()):
        raise PermissionError('Human approval requires a person at an interactive terminal')
    if method not in HUMAN_METHODS or not reviewer or not reviewer.strip():
        raise ValueError('Reviewer name required')
    return ReviewSession(reviewer.strip(), method, _TOKEN)


def synthetic_session():
    """For synthetic rehearsals and tests only; refused by non-synthetic ledgers."""
    return ReviewSession('SYNTHETIC FIXTURE (not a person)', 'synthetic_fixture', _TOKEN)


def iso_period(value):
    year, week, _ = timestamp(value).isocalendar()
    return f'{year}-W{week:02d}'


class Ledger:
    def __init__(self, path, tenant_id, synthetic, clock):
        require_tenant(tenant_id)
        self.path, self.tenant, self.clock = Path(path), tenant_id, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript(SCHEMA)
        meta = dict(self.db.execute('SELECT key, value FROM meta').fetchall())
        if not meta:
            with self.tx():
                self.db.executemany('INSERT INTO meta VALUES (?, ?)', [
                    ('tenant_id', tenant_id), ('synthetic', '1' if synthetic else '0'),
                    ('schema_version', SCHEMA_VERSION), ('created_at', self.now())])
            meta = {'tenant_id': tenant_id, 'synthetic': '1' if synthetic else '0'}
        if meta['tenant_id'] != tenant_id:
            self.db.close()
            raise ValueError(f'Ledger belongs to tenant {meta["tenant_id"]!r}, not {tenant_id!r}')
        if meta['synthetic'] != ('1' if synthetic else '0'):
            self.db.close()
            raise ValueError('Ledger synthetic flag does not match configuration')
        self.synthetic = synthetic

    # ------------------------------------------------------------ plumbing
    def now(self):
        return iso(self.clock())

    def tx(self):
        ledger = self

        class Tx:
            def __enter__(self):
                ledger.db.execute('BEGIN IMMEDIATE')

            def __exit__(self, kind, value, tb):
                ledger.db.execute('COMMIT' if kind is None else 'ROLLBACK')
                return False
        return Tx()

    def close(self):
        self.db.close()

    def _check_session(self, session):
        if not isinstance(session, ReviewSession):
            raise PermissionError('A review session is required')
        if session.method == 'synthetic_fixture' and not self.synthetic:
            raise PermissionError('Synthetic fixture approvals are refused in a real ledger')

    def _rows(self, sql, *args):
        return self.db.execute(sql, args).fetchall()

    # ------------------------------------------------------------ sources
    def sync_sources(self, allowlist, attested):
        if allowlist['tenant_id'] != self.tenant:
            raise ValueError('Allowlist tenant does not match ledger tenant')
        with self.tx():
            self.db.execute('UPDATE sources SET active=0 WHERE tenant_id=?', (self.tenant,))
            for entry in allowlist['pages']:
                rules = {n: {'case': r.get('case', 'sensitive')} for n, r in entry['fields'].items()}
                self.db.execute(
                    'INSERT INTO sources VALUES (?,?,?,?,?,?,?,1) ON CONFLICT(tenant_id, page_id) DO UPDATE SET '
                    'url=excluded.url, approved_by=excluded.approved_by, approved_at=excluded.approved_at, '
                    'rules=excluded.rules, access_attested=excluded.access_attested, active=1',
                    (self.tenant, entry['id'], entry['url'], entry['approved_by'], entry['approved_at'],
                     json.dumps(rules, sort_keys=True), 1 if attested(entry) else 0))

    def sources(self):
        return {r['page_id']: r for r in self._rows(
            'SELECT * FROM sources WHERE tenant_id=? AND active=1 ORDER BY page_id', self.tenant)}

    def rules(self, page_id):
        row = self.db.execute('SELECT rules FROM sources WHERE tenant_id=? AND page_id=?',
                              (self.tenant, page_id)).fetchone()
        return json.loads(row['rules']) if row else {}

    # ------------------------------------------------------------ observations
    @staticmethod
    def observation_id(record):
        return digest(record['tenant_id'], record['id'], record['url'], record['observed_at'],
                      record['capture_method'], record['status'], record.get('failure_reason'),
                      record.get('raw_facts', {}), record.get('content_sha256'), record.get('attempt', 1),
                      length=24)

    def _validate_record(self, record):
        if record.get('tenant_id') != self.tenant:
            raise ValueError('Record tenant_id is missing or belongs to another tenant')
        source = self.sources().get(record.get('id'))
        if source is None:
            raise ValueError('Page is not an active approved source for this tenant')
        if record['url'] != source['url']:
            raise ValueError('Record URL does not match the approved source URL')
        timestamp(record['observed_at'])
        if record['status'] not in ('ok', 'failed'):
            raise ValueError('Unknown record status')

    def has_observation(self, obs_id):
        return self.db.execute('SELECT 1 FROM observations WHERE obs_id=? AND tenant_id=?',
                               (obs_id, self.tenant)).fetchone() is not None

    def ingest(self, record, run_id=None, raw_dir=None):
        """Store an automated observation once and assess it. Returns (obs_id, created)."""
        self._validate_record(record)
        if record['capture_method'] != 'automated':
            raise ValueError('Manual observations go through ingest_manual with a review session')
        obs_id = self.observation_id(record)
        if self.has_observation(obs_id):
            return obs_id, False
        raw_path = None
        if raw_dir and record.get('untrusted_source_text'):
            raw_path = Path(raw_dir) / f'{obs_id}.txt'
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(record['untrusted_source_text'], encoding='utf-8')
        with self.tx():
            self._insert(obs_id, record, run_id, raw_path)
            status, reasons, acceptance = self._assess(obs_id)
            self.db.execute('UPDATE observations SET machine_status=?, machine_reasons=?, acceptance=?, accepted_rule=? '
                            'WHERE obs_id=?', (status, json.dumps(reasons), acceptance,
                                               AUTO_RULE if acceptance == 'auto_accepted' else None, obs_id))
            self._refresh(record['id'])
        return obs_id, True

    def ingest_manual(self, record, session):
        """A person's own observation: stored together with that person's review."""
        self._check_session(session)
        self._validate_record(record)
        if record['capture_method'] != 'manual':
            raise ValueError('Not a manual record')
        obs_id = self.observation_id(record)
        if self.has_observation(obs_id):
            return obs_id, False
        method = 'manual_entry' if session.method == 'interactive_cli' else session.method
        with self.tx():
            self._insert(obs_id, record, None, None)
            if record['status'] == 'ok':
                acceptance = 'fixture_accepted' if method == 'synthetic_fixture' else 'human_accepted'
                self.db.execute("UPDATE observations SET machine_status='manual', acceptance=? WHERE obs_id=?",
                                (acceptance, obs_id))
                self._supersede_older_pending(record['id'], record['observed_at'])
            self.db.execute('INSERT INTO reviews(tenant_id, obs_id, reviewer, method, reviewed_at, decision, corrections, '
                            "confirmed_absent, comment) VALUES (?,?,?,?,?,'accept','{}','[]','manual entry')",
                            (self.tenant, obs_id, session.reviewer, method, self.now()))
            self._refresh(record['id'])
        return obs_id, True

    def _insert(self, obs_id, record, run_id, raw_path):
        failed = record['status'] == 'failed'
        self.db.execute(
            'INSERT INTO observations(obs_id, tenant_id, page_id, url, observed_at, capture_method, check_status, '
            'failure_reason, raw_facts, facts, missing_fields, anomalies, evidence, source_times, content_sha256, '
            'text_length, machine_status, machine_reasons, acceptance, run_id, ingested_at, raw_text_path) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (obs_id, self.tenant, record['id'], record['url'], record['observed_at'], record['capture_method'],
             record['status'], record.get('failure_reason'), json.dumps(record.get('raw_facts', {}), sort_keys=True),
             json.dumps(record.get('facts', {}), sort_keys=True), json.dumps(record.get('missing_fields', [])),
             json.dumps(record.get('anomalies', [])), json.dumps(record.get('evidence', {}), sort_keys=True),
             json.dumps(record.get('source_times', {}), sort_keys=True), record.get('content_sha256'),
             record.get('text_length'), 'fetch_failed' if failed else 'unassessed', '[]',
             'not_applicable' if failed else 'pending_review', run_id, self.now(),
             str(raw_path) if raw_path else None))

    def observation(self, obs_id):
        row = self.db.execute('SELECT * FROM observations WHERE obs_id=? AND tenant_id=?', (obs_id, self.tenant)).fetchone()
        if row is None:
            raise ValueError('No such observation for this tenant')
        return row

    def set_note(self, obs_id, note):
        """Operator notes are presentation only: they never change identity or transitions."""
        self.observation(obs_id)
        with self.tx():
            self.db.execute('UPDATE observations SET note=? WHERE obs_id=?', (note, obs_id))

    # ------------------------------------------------------------ effective values & replay
    def _latest_review(self, obs_id):
        return self.db.execute("SELECT * FROM reviews WHERE obs_id=? AND decision IN ('accept','correct') "
                               'ORDER BY review_id DESC LIMIT 1', (obs_id,)).fetchone()

    def effective(self, row):
        """Accepted values: extracted facts, then reviewer corrections and confirmed absences."""
        facts = json.loads(row['facts'])
        absent = []
        review = self._latest_review(row['obs_id'])
        if review:
            facts.update(json.loads(review['corrections']))
            absent = json.loads(review['confirmed_absent'])
        values = {f: ('value', normalize(v)) for f, v in facts.items()}
        values.update({f: ('absent', None) for f in absent})
        return values

    def _accepted(self, page_id, before=None):
        sql = ('SELECT * FROM observations WHERE tenant_id=? AND page_id=? AND check_status=\'ok\' AND acceptance IN '
               "('auto_accepted','human_accepted','fixture_accepted')")
        args = [self.tenant, page_id]
        if before is not None:
            sql += ' AND observed_at < ?'
            args.append(before)
        return self._rows(sql + ' ORDER BY observed_at, obs_id', *args)

    def replay(self, page_id, before=None):
        """Walk accepted observations in time order. Returns (state, transitions)."""
        rules = self.rules(page_id)
        key = lambda f, v: None if v[0] == 'absent' else compare_key(f, v[1], rules)
        state, url, last_obs, transitions = None, None, None, []
        for row in self._accepted(page_id, before):
            values = self.effective(row)
            if state is None or row['url'] != url:
                kind = 'baseline' if state is None else 'source_changed'
                changes = [{'field': f, 'from': None, 'to': v[1], 'to_kind': v[0]} for f, v in sorted(values.items())]
                state, url = dict(values), row['url']
            else:
                changes = []
                for field, value in sorted(values.items()):
                    old = state.get(field)
                    if old is None or key(field, old) != key(field, value):
                        changes.append({'field': field, 'from': old[1] if old else None, 'to': value[1],
                                        'from_kind': old[0] if old else 'unknown', 'to_kind': value[0]})
                        state[field] = value
                kind = 'change' if changes else None
            if kind:
                identity = [(c['field'], key(c['field'], ('value', c['from'])) if c['from'] is not None else None,
                             key(c['field'], ('value', c['to'])) if c['to'] is not None else None) for c in changes]
                transitions.append({'transition_id': digest(self.tenant, page_id, kind, row['obs_id'], identity, length=24),
                                    'kind': kind, 'from_obs_id': last_obs, 'to_obs_id': row['obs_id'],
                                    'observed_at': row['observed_at'], 'changes': changes})
            last_obs = row['obs_id']
        return state or {}, transitions

    def _refresh(self, page_id):
        """Make the stored transitions match the replay (inside the caller's transaction)."""
        _, transitions = self.replay(page_id)
        live = set()
        for t in transitions:
            live.add(t['transition_id'])
            self.db.execute('INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?,\'active\',?,NULL) ON CONFLICT(transition_id) '
                            "DO UPDATE SET state='active', retracted_at=NULL",
                            (t['transition_id'], self.tenant, page_id, t['kind'], t['from_obs_id'], t['to_obs_id'],
                             t['observed_at'], json.dumps(t['changes'], sort_keys=True), self.now()))
        for row in self._rows("SELECT transition_id FROM transitions WHERE tenant_id=? AND page_id=? AND state='active'",
                              self.tenant, page_id):
            if row['transition_id'] not in live:
                self.db.execute("UPDATE transitions SET state='retracted', retracted_at=? WHERE transition_id=?",
                                (self.now(), row['transition_id']))

    def transitions(self, page_id=None, state=None):
        sql, args = 'SELECT * FROM transitions WHERE tenant_id=?', [self.tenant]
        if page_id:
            sql, args = sql + ' AND page_id=?', args + [page_id]
        if state:
            sql, args = sql + ' AND state=?', args + [state]
        return self._rows(sql + ' ORDER BY observed_at, transition_id', *args)

    # ------------------------------------------------------------ machine assessment
    def _assess(self, obs_id):
        row = self.observation(obs_id)
        if row['check_status'] == 'failed':
            return 'fetch_failed', [], 'not_applicable'
        prior = self._accepted(row['page_id'], before=row['observed_at'])
        approvals = [r for r in prior if r['acceptance'] in APPROVED]
        reasons = []
        if not approvals:
            reasons.append('baseline_requires_human_review')
        if prior and prior[-1]['url'] != row['url']:
            reasons.append('source_url_changed')
        state, _ = self.replay(row['page_id'], before=row['observed_at'])
        missing = [f for f in json.loads(row['missing_fields'])
                   if not (f in state and state[f][0] == 'absent')]  # absence a reviewer already confirmed
        if missing:
            reasons.append('incomplete_extraction')
        reasons += ['extraction_anomaly:' + a for a in json.loads(row['anomalies'])]
        if approvals and approvals[-1]['text_length'] and row['text_length']:
            ratio = row['text_length'] / approvals[-1]['text_length']
            if not TEXT_SHIFT_RANGE[0] <= ratio <= TEXT_SHIFT_RANGE[1]:
                reasons.append('extraction_anomaly:page_size_shift')
        rules = self.rules(row['page_id'])
        for field, value in (json.loads(row['facts']).items() if state else ()):  # no baseline: nothing to change from
            old = state.get(field)
            if old is None or old[0] == 'absent' or compare_key(field, old[1], rules) != compare_key(field, value, rules):
                reasons.append('material_change')
                break
        if approvals and age_hours(approvals[-1]['observed_at'], row['observed_at']) > REVERIFY_DAYS * 24:
            reasons.append('periodic_reverification_due')
        if age_hours(row['observed_at'], self.now()) > MAX_CAPTURE_AGE_HOURS:
            reasons.append('stale_capture')
        if not reasons:
            return 'unchanged_complete', [], 'auto_accepted'
        order = ['material_change', 'incomplete_extraction', 'baseline_requires_human_review', 'source_url_changed']
        status = next((r for r in order if r in reasons), reasons[0].split(':')[0])
        return status, reasons, 'pending_review'

    # ------------------------------------------------------------ review queue
    def review_queue(self):
        """One item per page: the newest pending capture; older pending ones are listed with it."""
        items = []
        for page_id in self.sources():
            pending = self._rows("SELECT * FROM observations WHERE tenant_id=? AND page_id=? AND acceptance='pending_review' "
                                 'ORDER BY observed_at DESC', self.tenant, page_id)
            if not pending:
                continue
            row = pending[0]
            state, _ = self.replay(page_id, before=row['observed_at'])
            items.append({'tenant_id': self.tenant, 'page_id': page_id, 'obs_id': row['obs_id'], 'url': row['url'],
                          'observed_at': row['observed_at'], 'reasons': json.loads(row['machine_reasons']),
                          'facts': json.loads(row['facts']), 'raw_facts': json.loads(row['raw_facts']),
                          'missing_fields': json.loads(row['missing_fields']),
                          'evidence': json.loads(row['evidence']),
                          'accepted_before': {f: v[1] for f, v in state.items()},
                          'older_pending': [r['obs_id'] for r in pending[1:]]})
        return items

    def _supersede_older_pending(self, page_id, observed_at):
        self.db.execute("UPDATE observations SET acceptance='superseded' WHERE tenant_id=? AND page_id=? "
                        "AND acceptance='pending_review' AND observed_at<?", (self.tenant, page_id, observed_at))

    def apply_review(self, session, obs_id, decision, corrections=None, confirmed_absent=None, comment=''):
        """Record a person's decision on a pending observation, or correct an accepted one."""
        self._check_session(session)
        row = self.observation(obs_id)
        corrections, confirmed_absent = dict(corrections or {}), list(confirmed_absent or [])
        fields = set(self.rules(row['page_id']))
        if set(corrections) - fields or set(confirmed_absent) - fields:
            raise ValueError('Corrections name fields this source does not track')
        if any(not normalize(v) for v in corrections.values()):
            raise ValueError('Empty corrections are not allowed; confirm absence explicitly instead')
        if row['check_status'] != 'ok':
            raise ValueError('Failed checks need no review')
        if decision == 'accept' and row['acceptance'] != 'pending_review':
            raise ValueError('Only pending observations can be accepted')
        if decision == 'correct' and row['acceptance'] not in ACCEPTED:
            raise ValueError('Only accepted observations can be corrected')
        if decision == 'reject' and row['acceptance'] != 'pending_review':
            raise ValueError('Only pending observations can be rejected')
        acceptance = {'accept': 'fixture_accepted' if session.method == 'synthetic_fixture' else 'human_accepted',
                      'reject': 'rejected', 'correct': row['acceptance']}[decision]
        if decision == 'correct' and row['acceptance'] == 'auto_accepted':
            acceptance = 'fixture_accepted' if session.method == 'synthetic_fixture' else 'human_accepted'
        with self.tx():
            self.db.execute('INSERT INTO reviews(tenant_id, obs_id, reviewer, method, reviewed_at, decision, corrections, '
                            'confirmed_absent, comment) VALUES (?,?,?,?,?,?,?,?,?)',
                            (self.tenant, obs_id, session.reviewer, session.method, self.now(), decision,
                             json.dumps(corrections, sort_keys=True), json.dumps(sorted(confirmed_absent)), comment))
            self.db.execute('UPDATE observations SET acceptance=? WHERE obs_id=?', (acceptance, obs_id))
            if decision == 'accept':
                self._supersede_older_pending(row['page_id'], row['observed_at'])
            self._reassess_later(row['page_id'], row['observed_at'])
            self._refresh(row['page_id'])

    def _reassess_later(self, page_id, observed_at):
        """Accepting or correcting an older observation changes the baseline for later automatic
        acceptances; any that no longer qualify go back to the review queue."""
        for later in self._rows("SELECT obs_id FROM observations WHERE tenant_id=? AND page_id=? AND observed_at>? "
                                "AND acceptance='auto_accepted' ORDER BY observed_at", self.tenant, page_id, observed_at):
            status, reasons, acceptance = self._assess(later['obs_id'])
            if acceptance != 'auto_accepted':
                self.db.execute('UPDATE observations SET machine_status=?, machine_reasons=?, acceptance=?, accepted_rule=NULL '
                                'WHERE obs_id=?', (status, json.dumps(reasons + ['baseline_changed_by_review']),
                                                   acceptance, later['obs_id']))

    # ------------------------------------------------------------ report preparation
    def _delivered(self, role):
        return {r['transition_id'] for r in self._rows(
            'SELECT i.transition_id FROM report_items i JOIN reports r USING(report_id) '
            "WHERE r.tenant_id=? AND r.state='delivered_ack' AND i.role=?", self.tenant, role)}

    def _unknown_delivery(self):
        found = {}
        for r in self._rows('SELECT i.transition_id, r.report_id FROM report_items i JOIN reports r USING(report_id) '
                            "WHERE r.tenant_id=? AND r.state IN ('delivery_unknown','released') AND i.role='event'",
                            self.tenant):
            found.setdefault(r['transition_id'], []).append(r['report_id'])
        return found

    def _acceptance_label(self, row):
        if row['acceptance'] == 'auto_accepted':
            return f'Automatically re-checked; matched accepted values (rule {row["accepted_rule"]}). Not individually human-reviewed.'
        review = self._latest_review(row['obs_id'])
        if row['acceptance'] == 'fixture_accepted':
            return 'Synthetic fixture approval (not a person).'
        if review and review['method'] == 'manual_entry':
            return f'Entered manually by {review["reviewer"]} on {review["reviewed_at"]}.'
        if review:
            return f'Human-reviewed by {review["reviewer"]} on {review["reviewed_at"]}.'
        return 'Accepted.'

    def page_states(self, as_of, included):
        window_start = iso(timestamp(as_of) - timedelta(days=REPORT_WINDOW_DAYS))
        pages = []
        for page_id, source in self.sources().items():
            rows = self._rows("SELECT * FROM observations WHERE tenant_id=? AND page_id=? AND observed_at<=? "
                              "AND acceptance!='superseded' ORDER BY observed_at, obs_id", self.tenant, page_id, as_of)
            accepted = [r for r in rows if r['acceptance'] in ACCEPTED]
            last_ok = accepted[-1] if accepted else None
            pending = [r for r in rows if r['acceptance'] == 'pending_review'
                       and (last_ok is None or r['observed_at'] > last_ok['observed_at'])]
            latest = rows[-1] if rows else None
            in_window = [r for r in rows if r['observed_at'] >= window_start]
            reason, reasons = '', []
            if pending:
                reasons = json.loads(pending[-1]['machine_reasons'])
                state = 'INCOMPLETE' if 'incomplete_extraction' in reasons else 'PENDING REVIEW'
                reason = 'Newer capture from ' + pending[-1]['observed_at'] + ' awaits review: ' + ', '.join(reasons)
            elif not in_window:
                if not source['access_attested']:
                    state, reason = 'NOT CHECKED', 'Access rules for this page have not been attested by a person; no request was made.'
                elif last_ok:
                    state, reason = 'STALE', f'No check in the last {REPORT_WINDOW_DAYS} days.'
                else:
                    state, reason = 'NOT CHECKED', 'No check has been made yet.'
            elif latest['check_status'] == 'failed' or latest['acceptance'] == 'rejected':
                why = latest['failure_reason'] or 'capture rejected by reviewer'
                fresh = last_ok and age_hours(last_ok['observed_at'], as_of) <= MAX_CAPTURE_AGE_HOURS
                state = 'CHECK FAILED' if fresh or not last_ok else 'STALE'
                reason = f'Latest check at {latest["observed_at"]} failed: {why}.'
            elif last_ok and age_hours(last_ok['observed_at'], as_of) > MAX_CAPTURE_AGE_HOURS:
                state, reason = 'STALE', f'No accepted observation within {MAX_CAPTURE_AGE_HOURS} hours.'
            else:
                kinds = {t['kind'] for t in included if t['page_id'] == page_id}
                state = ('SOURCE CHANGED' if 'source_changed' in kinds else 'CHANGED' if 'change' in kinds
                         else 'BASELINE' if 'baseline' in kinds else 'UNCHANGED')
            values = {}
            if last_ok:
                for field, (kind, value) in sorted(self.effective(last_ok).items()):
                    values[field] = {'value': value, 'kind': kind}
            pages.append({
                'page_id': page_id, 'url': source['url'], 'state': state, 'reason': reason, 'reasons': reasons,
                'last_checked_at': latest['observed_at'] if latest else None,
                'accepted_observed_at': last_ok['observed_at'] if last_ok else None,
                'accepted_age_days': round(age_hours(last_ok['observed_at'], as_of) / 24, 1) if last_ok else None,
                'accepted_by': self._acceptance_label(last_ok) if last_ok else None,
                'accepted_values': values,
                'tracked_fields': sorted(json.loads(source['rules'])),
                'evidence': json.loads(last_ok['evidence']) if last_ok else {}})
        return pages

    def prepare_report(self, as_of, out_dir, render):
        """Write a draft for the ISO week of as_of. Re-running with no new content is a no-op."""
        timestamp(as_of)
        period = iso_period(as_of)
        delivered, corrected = self._delivered('event'), self._delivered('retraction')
        unknown = self._unknown_delivery()
        events = [dict(t) for t in self.transitions(state='active')
                  if t['transition_id'] not in delivered and t['observed_at'] <= as_of]
        retractions = [dict(t) for t in self.transitions(state='retracted')
                       if t['transition_id'] in delivered and t['transition_id'] not in corrected]
        for t in events + retractions:
            t['changes'] = json.loads(t['changes'])
            t['possible_duplicate_of'] = unknown.get(t['transition_id'], [])
        pages = self.page_states(as_of, events)
        queue = self.review_queue()
        content = {'tenant_id': self.tenant, 'synthetic': self.synthetic, 'period': period, 'as_of': as_of,
                   'pages': pages, 'events': events, 'corrections': retractions,
                   'pending_review': [{'page_id': q['page_id'], 'reasons': q['reasons']} for q in queue],
                   'unconfirmed_deliveries': [dict(r) for r in self._rows(
                       "SELECT report_id, state, released_at FROM reports WHERE tenant_id=? AND state IN "
                       "('released','delivery_unknown') ORDER BY prepared_at", self.tenant)]}
        content_hash = digest(content, length=64)
        last = self.db.execute("SELECT * FROM reports WHERE tenant_id=? AND period=? AND state!='void' "
                               'ORDER BY revision DESC LIMIT 1', (self.tenant, period)).fetchone()
        if last and last['content_sha256'] == content_hash:
            return last['report_id'], False
        revision = (self.db.execute('SELECT MAX(revision) FROM reports WHERE tenant_id=? AND period=?',
                                    (self.tenant, period)).fetchone()[0] or 0) + 1
        report_id = f'{period}-r{revision}'
        content['report_id'] = report_id
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        html_path, json_path = out / f'{self.tenant}-{report_id}-DRAFT.html', out / f'{self.tenant}-{report_id}-DRAFT.json'
        html_path.write_text(render(content), encoding='utf-8')
        json_path.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding='utf-8')
        with self.tx():
            self.db.execute("UPDATE reports SET state='void' WHERE tenant_id=? AND period=? AND state='draft'",
                            (self.tenant, period))
            self.db.execute('INSERT INTO reports(report_id, tenant_id, period, revision, as_of, prepared_at, content_sha256, '
                            "html_path, json_path, state) VALUES (?,?,?,?,?,?,?,?,?,'draft')",
                            (report_id, self.tenant, period, revision, as_of, self.now(), content_hash,
                             str(html_path), str(json_path)))
            self.db.executemany('INSERT INTO report_items VALUES (?,?,?)',
                                [(report_id, t['transition_id'], 'event') for t in events]
                                + [(report_id, t['transition_id'], 'retraction') for t in retractions])
        return report_id, True

    def report(self, report_id):
        row = self.db.execute('SELECT * FROM reports WHERE report_id=? AND tenant_id=?', (report_id, self.tenant)).fetchone()
        if row is None:
            raise ValueError('No such report for this tenant')
        return row

    def release(self, session, report_id):
        """Person approves a draft for sending. This does not send anything."""
        self._check_session(session)
        if self.report(report_id)['state'] != 'draft':
            raise ValueError('Only a current draft can be released')
        with self.tx():
            self.db.execute("UPDATE reports SET state='released', released_by=?, released_at=?, release_method=? "
                            'WHERE report_id=?', (session.reviewer, self.now(), session.method, report_id))

    def acknowledge_delivery(self, session, report_id, channel, note=''):
        """Person states they sent a released report themselves. Nothing is sent by this code."""
        self._check_session(session)
        if self.report(report_id)['state'] not in ('released', 'delivery_unknown'):
            raise ValueError('Only a released (or delivery-unknown) report can be acknowledged as delivered')
        if not channel.strip():
            raise ValueError('Say how it was sent, e.g. "email from my mailbox"')
        with self.tx():
            self.db.execute("UPDATE reports SET state='delivered_ack', delivery_ack_by=?, delivery_ack_at=?, "
                            'delivery_channel=?, delivery_note=? WHERE report_id=?',
                            (session.reviewer, self.now(), channel, note, report_id))

    def mark_delivery_unknown(self, session, report_id, note=''):
        self._check_session(session)
        if self.report(report_id)['state'] != 'released':
            raise ValueError('Only a released report can have unknown delivery')
        with self.tx():
            self.db.execute("UPDATE reports SET state='delivery_unknown', delivery_note=? WHERE report_id=?",
                            (note, report_id))

    # ------------------------------------------------------------ status & retention
    def summary(self):
        counts = {r['state']: r['n'] for r in self._rows(
            'SELECT state, COUNT(*) n FROM reports WHERE tenant_id=? GROUP BY state', self.tenant)}
        latest = self.db.execute("SELECT report_id, html_path FROM reports WHERE tenant_id=? AND state='draft' "
                                 'ORDER BY prepared_at DESC LIMIT 1', (self.tenant,)).fetchone()
        return {'tenant_id': self.tenant, 'pending_review': len(self.review_queue()), 'reports': counts,
                'current_draft': dict(latest) if latest else None}

    def prune(self, policy, apply=False):
        """Deletes only raw page text and stale draft files. Ledger rows are never deleted:
        they are needed for pending work, duplicate prevention and A->B->A detection."""
        now = self.clock()
        raw_cut = iso(now - timedelta(days=policy['raw_text_days']))
        draft_cut = iso(now - timedelta(days=policy['draft_report_days']))
        raw = self._rows("SELECT obs_id, raw_text_path FROM observations WHERE tenant_id=? AND raw_text_path IS NOT NULL "
                         "AND observed_at<? AND acceptance!='pending_review'", self.tenant, raw_cut)
        drafts = self._rows("SELECT report_id, html_path, json_path FROM reports WHERE tenant_id=? AND files_pruned=0 "
                            "AND state IN ('draft','void') AND prepared_at<?", self.tenant, draft_cut)
        plan = [r['raw_text_path'] for r in raw] + [p for r in drafts for p in (r['html_path'], r['json_path'])]
        if apply:
            with self.tx():
                for r in raw:
                    Path(r['raw_text_path']).unlink(missing_ok=True)
                    self.db.execute('UPDATE observations SET raw_text_path=NULL WHERE obs_id=?', (r['obs_id'],))
                for r in drafts:
                    for p in (r['html_path'], r['json_path']):
                        Path(p).unlink(missing_ok=True)
                    self.db.execute('UPDATE reports SET files_pruned=1 WHERE report_id=?', (r['report_id'],))
        return plan
