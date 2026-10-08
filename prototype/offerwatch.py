"""OfferWatch operator command (Python 3.10+, stdlib only).

`run` collects due pages, validates, compares and prepares DRAFT reports. Nothing is ever
sent: there is no email, upload or notification code in this project.
Operational data lives outside the repository (default %LOCALAPPDATA%\\OfferWatch on Windows).
"""
import argparse
import hashlib
import json
import ntpath
import os
import platform
import secrets
import shutil
import sqlite3
import ssl
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import capture
import report
import store
from owcore import iso, require_tenant, timestamp

EXIT_OK, EXIT_ERROR, EXIT_PARTIAL, EXIT_LOCKED = 0, 1, 2, 3
LOCK_STALE_SECONDS = 2 * 3600
ATTEMPTS_PER_RUN = 2
FAILED_ATTEMPTS_PER_SLOT = 4
FAILURE_BACKOFF_HOURS = 2
MIN_SUCCESS_SPACING_HOURS = 24
PAUSE_SECONDS = 2
RETRY_PAUSE_SECONDS = 30
LOG_MAX_BYTES = 1_000_000


class Locked(Exception):
    pass


# ---------------------------------------------------------------- data directory

def default_data_dir():
    if os.environ.get('OFFERWATCH_HOME'):
        return Path(os.environ['OFFERWATCH_HOME'])
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'OfferWatch'
    return Path.home() / '.offerwatch'


def git_worktree(path):
    path = Path(path).resolve()
    for parent in [path] + list(path.parents):
        if (parent / '.git').exists():
            return parent
    return None


class DataDir:
    def __init__(self, root):
        self.root = Path(root).resolve()
        repo = git_worktree(self.root)
        if repo:
            raise ValueError(f'Refusing to keep operational data inside a git working tree ({repo})')
        self.root.mkdir(parents=True, exist_ok=True)

    def tenant_dir(self, tenant):
        return self.root / 'tenants' / require_tenant(tenant)

    def tenants(self):
        base = self.root / 'tenants'
        return sorted(p.name for p in base.iterdir() if (p / 'allowlist.json').exists()) if base.exists() else []

    def allowlist(self, tenant):
        data = json.loads((self.tenant_dir(tenant) / 'allowlist.json').read_text(encoding='utf-8'))
        capture.load_allowlist(data)
        if data['tenant_id'] != tenant:
            raise ValueError(f'Allowlist in folder {tenant!r} names tenant {data["tenant_id"]!r}')
        return data

    def save_allowlist(self, data):
        capture.load_allowlist(data)
        path = self.tenant_dir(data['tenant_id']) / 'allowlist.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, path)

    def ledger(self, tenant, allowlist, clock):
        return store.Ledger(self.tenant_dir(tenant) / 'ledger.sqlite3', tenant, allowlist['synthetic'], clock)

    def retention(self):
        path = self.root / 'retention.json'
        policy = dict(store.DEFAULT_RETENTION)
        if path.exists():
            policy.update(json.loads(path.read_text(encoding='utf-8')))
        return policy


class RunLock:
    """Prevents overlapping runs. A lock older than LOCK_STALE_SECONDS is treated as left by a
    killed process and set aside (renamed, not deleted). PIDs are not probed: on Windows,
    os.kill(pid, 0) would terminate the process."""

    def __init__(self, root, clock=time.time):
        self.path, self.clock, self.recovered = Path(root) / 'run.lock', clock, None

    def __enter__(self):
        for attempt in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                age = self.clock() - self.path.stat().st_mtime
                if attempt or age < LOCK_STALE_SECONDS:
                    raise Locked(f'Another run holds {self.path} (age {age / 60:.0f} min)')
                self.recovered = self.path.with_name(f'run.lock.stale-{int(self.clock())}')
                os.replace(self.path, self.recovered)
                continue
            with os.fdopen(fd, 'w') as handle:
                handle.write(json.dumps({'pid': os.getpid(), 'started': self.clock()}))
            return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
        return False


# ---------------------------------------------------------------- scheduling logic

def slot_start(moment):
    """Two collection slots per ISO week (UTC): Mon-Wed and Thu-Sun."""
    monday = (moment - timedelta(days=moment.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday + timedelta(days=3) if moment.weekday() >= 3 else monday


def due(ledger, page_id, now):
    rows = ledger._rows('SELECT observed_at, check_status, capture_method FROM observations WHERE tenant_id=? '
                        'AND page_id=? ORDER BY observed_at', ledger.tenant, page_id)
    start = iso(slot_start(now))
    in_slot = [r for r in rows if r['observed_at'] >= start]
    if any(r['check_status'] == 'ok' for r in in_slot):
        return False, 'already captured in this slot'
    ok = [r for r in rows if r['check_status'] == 'ok' and r['capture_method'] == 'automated']
    if ok and (now - timestamp(ok[-1]['observed_at'])).total_seconds() < MIN_SUCCESS_SPACING_HOURS * 3600:
        return False, f'captured less than {MIN_SUCCESS_SPACING_HOURS}h ago'
    failures = [r for r in in_slot if r['check_status'] == 'failed']
    if len(failures) >= FAILED_ATTEMPTS_PER_SLOT:
        return False, 'retry budget for this slot is spent'
    if failures and (now - timestamp(failures[-1]['observed_at'])).total_seconds() < FAILURE_BACKOFF_HOURS * 3600:
        return False, 'recent failure; backing off'
    return True, 'due'


# ---------------------------------------------------------------- the one command

def run_pipeline(data, transport=capture.https_get, clock=capture.now_utc, sleep=time.sleep,
                 tenants=None, force=False, as_of=None):
    run_id = iso(clock()) + '-' + secrets.token_hex(3)
    started, wall = clock(), time.perf_counter()
    summary = {'run_id': run_id, 'started_at': iso(started), 'tenants': {}, 'failures': [], 'errors': [],
               'requests': 0}

    real = getattr(transport, 'real_network', False)

    def fetch(*args, **kwargs):
        summary['requests'] += 1
        return transport(*args, **kwargs)
    fetch.real_network = real

    for tenant in tenants or data.tenants():
        info = {'captured': 0, 'failed': 0, 'skipped': {}, 'pending_review': 0, 'draft': None}
        summary['tenants'][tenant] = info
        ledger = None
        try:
            allowlist = data.allowlist(tenant)
            if real and allowlist['synthetic']:
                raise ValueError('Synthetic tenant: network collection refused')
            ledger = data.ledger(tenant, allowlist, clock)
            ledger.sync_sources(allowlist, capture.access_attested)
            cache, first = {}, True
            for entry in allowlist['pages']:
                if not capture.access_attested(entry):
                    info['skipped'][entry['id']] = 'access rules not attested; no request made'
                    continue
                ok, why = (True, 'forced') if force else due(ledger, entry['id'], clock())
                if not ok:
                    info['skipped'][entry['id']] = why
                    continue
                for attempt in range(ATTEMPTS_PER_RUN):
                    if not first:
                        sleep(PAUSE_SECONDS)
                    first = False
                    record = capture.capture_page(allowlist, entry, transport=fetch, robots_cache=cache, clock=clock)
                    record['attempt'] = attempt + 1  # distinct identity for same-second retries
                    ledger.ingest(record, run_id, raw_dir=data.tenant_dir(tenant) / 'raw')
                    if record['status'] == 'ok':
                        info['captured'] += 1
                        break
                    if not capture.retryable(record) or attempt == ATTEMPTS_PER_RUN - 1:
                        info['failed'] += 1
                        summary['failures'].append({'tenant': tenant, 'page': entry['id'],
                                                    'reason': record['failure_reason'], 'attempts': attempt + 1})
                        break
                    sleep(RETRY_PAUSE_SECONDS)
            report_id, _ = ledger.prepare_report(as_of or iso(clock()), data.tenant_dir(tenant) / 'reports', report.render)
            status = ledger.summary()
            info.update(pending_review=status['pending_review'], draft=status['current_draft'],
                        unconfirmed=status['reports'].get('released', 0) + status['reports'].get('delivery_unknown', 0))
        except Exception as exc:  # one tenant's problem must not stop the others
            summary['errors'].append(f'{tenant}: {type(exc).__name__}: {exc}')
        finally:
            if ledger:
                ledger.close()
    summary['finished_at'] = iso(clock())
    summary['computer_seconds'] = round(time.perf_counter() - wall, 3)
    summary['outcome'] = 'ERROR' if summary['errors'] else 'COMPLETED WITH FAILURES' if summary['failures'] else 'OK'
    write_status(data, summary)
    return summary


def write_status(data, summary):
    path = data.root / 'status.json'
    previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    last_success = summary['finished_at'] if summary['outcome'] == 'OK' else previous.get('last_success_at')
    status = dict(summary, last_success_at=last_success)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(status, indent=2), encoding='utf-8')
    os.replace(tmp, path)
    lines = [f'OfferWatch status (written {summary["finished_at"]})',
             f'Last run: {summary["run_id"]}  outcome: {summary["outcome"]}',
             f'Last fully successful run: {last_success or "never"}',
             f'Computer time: {summary["computer_seconds"]} s, network requests: {summary["requests"]}', '']
    for tenant, info in summary['tenants'].items():
        draft = info.get('draft') or {}
        lines.append(f'{tenant}: captured {info["captured"]}, failed {info["failed"]}, skipped {len(info["skipped"])}, '
                     f'review queue {info["pending_review"]}, draft {draft.get("report_id", "-")}')
        if info.get('unconfirmed'):
            lines.append(f'  {info["unconfirmed"]} released report(s) not confirmed delivered')
    lines.append('')
    lines += ['FAILURES NEEDING ATTENTION:'] + [f'  - {f["tenant"]} / {f["page"]}: {f["reason"]} '
                                             f'({f["attempts"]} attempt(s))' for f in summary['failures']] \
        if summary['failures'] else ['No page failures.']
    lines += ['ERRORS:'] + [f'  - {e}' for e in summary['errors']] if summary['errors'] else []
    lines += ['', 'Nothing was sent. Drafts need: offerwatch.py review, then release, then manual send + ack-delivery.']
    (data.root / 'STATUS.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    log = data.root / 'logs' / 'runs.log'
    log.parent.mkdir(exist_ok=True)
    if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
        os.replace(log, log.with_suffix('.log.1'))
    entry = {k: summary[k] for k in ('run_id', 'started_at', 'finished_at', 'outcome', 'computer_seconds', 'requests',
                                     'failures', 'errors')}
    with log.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry) + '\n')


# ---------------------------------------------------------------- backup / restore

def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def backup(data, dest, clock=capture.now_utc):
    dest = Path(dest).resolve()
    if git_worktree(dest):
        raise ValueError('Refusing to write a backup inside a git working tree')
    target = dest / ('offerwatch-backup-' + iso(clock()).replace(':', '').replace('-', ''))
    with RunLock(data.root):
        for tenant in data.tenants():
            src_dir, out_dir = data.tenant_dir(tenant), target / 'tenants' / tenant
            out_dir.mkdir(parents=True)
            shutil.copy2(src_dir / 'allowlist.json', out_dir / 'allowlist.json')
            if (src_dir / 'ledger.sqlite3').exists():
                src, dst = sqlite3.connect(str(src_dir / 'ledger.sqlite3')), sqlite3.connect(str(out_dir / 'ledger.sqlite3'))
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
            for sub in ('raw', 'reports'):
                if (src_dir / sub).exists():
                    shutil.copytree(src_dir / sub, out_dir / sub)
    files = {str(p.relative_to(target)).replace('\\', '/'): _sha256(p) for p in sorted(target.rglob('*')) if p.is_file()}
    (target / 'manifest.json').write_text(json.dumps({'created_at': iso(clock()), 'files': files}, indent=2), encoding='utf-8')
    return target


def verify_backup(source):
    source = Path(source)
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    for rel, digest_ in manifest['files'].items():
        if _sha256(source / rel) != digest_:
            raise ValueError('Backup file changed or damaged: ' + rel)
    for db in (source / 'tenants').glob('*/ledger.sqlite3'):
        conn = sqlite3.connect(str(db))
        try:
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Integrity check failed: ' + str(db))
            if conn.execute("SELECT value FROM meta WHERE key='tenant_id'").fetchone()[0] != db.parent.name:
                raise ValueError('Tenant mismatch in backup: ' + db.parent.name)
        finally:
            conn.close()
    return manifest


def restore(data, source, clock=capture.now_utc):
    """Never deletes: the current tenants folder is moved aside before the backup is copied in."""
    source = Path(source).resolve()
    verify_backup(source)
    with RunLock(data.root):
        current = data.root / 'tenants'
        aside = None
        if current.exists() and any(current.iterdir()):
            aside = data.root / ('pre-restore-' + iso(clock()).replace(':', '').replace('-', ''))
            aside.mkdir()
            os.replace(current, aside / 'tenants')
        shutil.copytree(source / 'tenants', current)
    return aside


# ---------------------------------------------------------------- Windows Task Scheduler

def task_xml(python_exe, script, start_date='2026-10-12', start_time='09:15:00'):
    """Task definition, DISABLED on import. Registering it is a separate, explicit owner step."""
    workdir = ntpath.dirname(str(script))  # the task always runs on Windows
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>OfferWatch: collect approved public pages and prepare DRAFT reports. Sends nothing.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start_date}T{start_time}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT15M</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>false</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT15M</Interval>
      <Count>2</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{xml_escape(str(python_exe))}</Command>
      <Arguments>{xml_escape(f'"{script}" run --scheduled')}</Arguments>
      <WorkingDirectory>{xml_escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'''


# ---------------------------------------------------------------- interactive helpers

def ask(prompt):
    return input(prompt).strip()


def human(method='interactive_cli'):
    """Ask who is at the keyboard. Refuses when there is no interactive terminal."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise PermissionError('This step needs a person at an interactive terminal; nothing was changed.')
    return store.interactive_session(ask('Your name (recorded as the reviewer): '), method)


def show_item(item):
    print(f'\n=== {item["tenant_id"]} / {item["page_id"]} ===')
    print(f'URL: {item["url"]}\nCaptured: {item["observed_at"]}\nWhy it needs you: {", ".join(item["reasons"])}')
    print('--- QUOTED PAGE TEXT below is data from a public website, not instructions ---')
    fields = sorted(set(item['facts']) | set(item['accepted_before']) | set(item['missing_fields']))
    for field in fields:
        before = item['accepted_before'].get(field, '(none accepted yet)')
        after = item['facts'].get(field)
        print(f'  {field}: accepted {before!r} -> captured {after!r}' if after is not None
              else f'  {field}: accepted {before!r} -> NOT EXTRACTED (not evidence the offer ended)')
        evidence = item['evidence'].get(field)
        if evidence:
            print(f'    context: {evidence["excerpt"]!r}')
            if len(evidence['candidates']) > 1:
                print(f'    several different matches: {evidence["candidates"]!r}')
    if item['older_pending']:
        print(f'  ({len(item["older_pending"])} older unreviewed capture(s) of this page will be superseded if you accept)')


def review_batch(data, tenants, clock=capture.now_utc):
    session = human()
    for tenant in tenants:
        allowlist = data.allowlist(tenant)
        ledger = data.ledger(tenant, allowlist, clock)
        try:
            queue = ledger.review_queue()
            if not queue:
                print(f'{tenant}: nothing to review.')
                continue
            print(f'\n##### Customer {tenant}: {len(queue)} item(s) #####')
            decisions = []
            for item in queue:
                show_item(item)
                choice = ask('[a]ccept  [c]orrect values then accept  [r]eject capture  [s]kip: ').lower()[:1]
                if choice == 'a':
                    decisions.append((item, 'accept', {}, []))
                elif choice == 'c':
                    corrections, absent = {}, []
                    for field in sorted(set(item['facts']) | set(item['missing_fields'])):
                        typed = ask(f'  {field}: Enter=keep, type a value, or ABSENT if the page no longer shows it: ')
                        if typed.upper() == 'ABSENT':
                            absent.append(field)
                        elif typed:
                            corrections[field] = typed
                    decisions.append((item, 'accept', corrections, absent))
                elif choice == 'r':
                    decisions.append((item, 'reject', {}, []))
            if not decisions:
                continue
            print(f'\n{len(decisions)} decision(s) for {tenant}.')
            if ask(f'Type the customer id ({tenant}) to record them: ') != tenant:
                print('Not recorded.')
                continue
            for item, decision, corrections, absent in decisions:
                ledger.apply_review(session, item['obs_id'], decision, corrections, absent)
            report_id, _ = ledger.prepare_report(iso(clock()), data.tenant_dir(tenant) / 'reports', report.render)
            print(f'Recorded. Current draft: {report_id}')
        finally:
            ledger.close()


# ---------------------------------------------------------------- doctor

def doctor(data_dir):
    lines, problems = [], []
    lines.append(f'Python {platform.python_version()} on {platform.system()} {platform.release()} ({sys.executable})')
    if sys.version_info < (3, 10):
        problems.append('Python 3.10 or newer is required')
    lines.append(f'SQLite {sqlite3.sqlite_version}')
    stats = ssl.create_default_context().cert_store_stats()
    lines.append(f'TLS trust store: {stats.get("x509_ca", 0)} CA certificates loaded')
    if os.name == 'nt':
        pyw = Path(sys.executable).with_name('pythonw.exe')
        lines.append(f'pythonw.exe: {"found" if pyw.exists() else "NOT FOUND"} at {pyw}')
        if not pyw.exists():
            problems.append('pythonw.exe not found next to python.exe (needed for the scheduled task)')
    try:
        data = DataDir(data_dir)
        probe = data.root / '.write-test'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
        lines.append(f'Data folder: {data.root} (writable, outside any git repository)')
        for tenant in data.tenants():
            try:
                allowlist = data.allowlist(tenant)
                attested = sum(1 for e in allowlist['pages'] if capture.access_attested(e))
                contact = 'OWNER_CONTACT' not in allowlist.get('user_agent', '') and bool(allowlist.get('user_agent'))
                lines.append(f'  customer {tenant}: {len(allowlist["pages"])} pages, {attested} attested, '
                             f'contact {"set" if contact else "MISSING"}, synthetic={allowlist["synthetic"]}')
            except Exception as exc:
                problems.append(f'customer {tenant}: {exc}')
        if not data.tenants():
            lines.append('  no customers configured yet (expected before onboarding)')
    except Exception as exc:
        problems.append(f'Data folder: {exc}')
    import rehearsal
    with tempfile.TemporaryDirectory() as tmp:
        result = rehearsal.run_rehearsal(Path(tmp) / 'data', Path(tmp) / 'out')
    lines.append(f'Offline rehearsal: {"PASSED" if result["checks_passed"] else "FAILED"} '
                 f'({result["computer_seconds_total"]} s computer time for {len(result["runs"])} runs)')
    if not result['checks_passed']:
        problems.append('offline rehearsal checks failed: ' + ', '.join(result['failed_checks']))
    lines.append('No network requests were made by doctor.')
    lines += ['PROBLEMS:'] + [f'  - {p}' for p in problems] if problems else ['No problems found.']
    return '\n'.join(lines), not problems


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--data-dir', default=None, help='default: %%LOCALAPPDATA%%\\OfferWatch or ~/.offerwatch')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('run', help='collect due pages, compare, prepare drafts (never sends)')
    p.add_argument('--tenant', action='append')
    p.add_argument('--force', action='store_true', help='ignore slot/backoff rules (still 1 request per page)')
    p.add_argument('--scheduled', action='store_true', help='set by the scheduled task')
    sub.add_parser('status')
    p = sub.add_parser('review', help='interactive batch review of exceptions')
    p.add_argument('--tenant', action='append')
    p = sub.add_parser('prepare', help='prepare drafts from the ledger without collecting')
    p.add_argument('--tenant', action='append')
    for name in ('release', 'ack-delivery', 'delivery-unknown'):
        p = sub.add_parser(name)
        p.add_argument('--tenant', required=True)
        p.add_argument('--report', required=True)
        if name == 'ack-delivery':
            p.add_argument('--channel', required=True, help='how you sent it, e.g. "email from my mailbox"')
    p = sub.add_parser('import', help='record your own observation of a page (interactive)')
    p.add_argument('--tenant', required=True)
    p.add_argument('--page', required=True)
    p.add_argument('--observed-at', required=True)
    p.add_argument('--field', action='append', default=[], metavar='NAME=VALUE')
    p.add_argument('--failed', metavar='REASON')
    p = sub.add_parser('add-source', help='create or update a customer allowlist entry (validated)')
    p.add_argument('--tenant', required=True)
    p.add_argument('--market', default='')
    p.add_argument('--contact', help='your business contact, e.g. mailto:you@yourdomain')
    p.add_argument('--id', required=True)
    p.add_argument('--url', required=True)
    p.add_argument('--approved-by', required=True, help='customer contact who approved monitoring this page')
    p.add_argument('--approved-at', required=True)
    p.add_argument('--field', action='append', default=[], metavar='NAME=REGEX')
    p.add_argument('--case-insensitive', action='append', default=[], metavar='NAME')
    p = sub.add_parser('attest-access', help='record that you checked terms/login/CAPTCHA for a page (interactive)')
    p.add_argument('--tenant', required=True)
    p.add_argument('--id', required=True)
    p = sub.add_parser('backup')
    p.add_argument('--dest', required=True)
    p = sub.add_parser('restore')
    p.add_argument('--from', dest='source', required=True)
    p = sub.add_parser('prune')
    p.add_argument('--apply', action='store_true')
    p = sub.add_parser('task-xml', help='write a DISABLED Task Scheduler definition (does not install it)')
    p.add_argument('--out', required=True)
    p.add_argument('--python', help='pythonw.exe path (default: next to this python.exe)')
    sub.add_parser('doctor', help='offline environment check + synthetic rehearsal; no network')
    p = sub.add_parser('rehearsal', help='offline two-customer rehearsal with synthetic pages')
    p.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()

    if args.command == 'doctor':
        text, ok = doctor(data_dir)
        print(text)
        return EXIT_OK if ok else EXIT_ERROR
    if args.command == 'rehearsal':
        import rehearsal
        out = Path(args.out)
        result = rehearsal.run_rehearsal(out / 'data', out)
        print(rehearsal.summary_text(result))
        return EXIT_OK if result['checks_passed'] else EXIT_ERROR
    if args.command == 'task-xml':
        script = Path(__file__).resolve()
        python = args.python or str(Path(sys.executable).with_name('pythonw.exe'))
        Path(args.out).write_text(task_xml(python, script), encoding='utf-16')
        print(f'Wrote {args.out} (task is DISABLED; nothing was installed).')
        return EXIT_OK

    data = DataDir(data_dir)
    tenants = getattr(args, 'tenant', None)
    if isinstance(tenants, str):
        tenants = [tenants]
    if args.command == 'run':
        try:
            with RunLock(data.root) as lock:
                summary = run_pipeline(data, tenants=tenants, force=args.force)
                if lock.recovered:
                    print(f'Recovered a stale lock left by an interrupted run ({lock.recovered.name}).')
        except Locked as exc:
            print(str(exc))
            return EXIT_LOCKED
        print((data.root / 'STATUS.txt').read_text(encoding='utf-8'))
        return {'OK': EXIT_OK, 'COMPLETED WITH FAILURES': EXIT_PARTIAL}.get(summary['outcome'], EXIT_ERROR)
    if args.command == 'status':
        path = data.root / 'STATUS.txt'
        print(path.read_text(encoding='utf-8') if path.exists() else 'No run has completed yet.')
        for tenant in data.tenants():
            allowlist = data.allowlist(tenant)
            ledger = data.ledger(tenant, allowlist, capture.now_utc)
            print(json.dumps(ledger.summary()))
            ledger.close()
        return EXIT_OK
    if args.command == 'review':
        with RunLock(data.root):
            review_batch(data, tenants or data.tenants())
        return EXIT_OK
    if args.command == 'prepare':
        with RunLock(data.root):
            for tenant in tenants or data.tenants():
                ledger = data.ledger(tenant, data.allowlist(tenant), capture.now_utc)
                print(tenant, ledger.prepare_report(iso(capture.now_utc()), data.tenant_dir(tenant) / 'reports',
                                                    report.render))
                ledger.close()
        return EXIT_OK
    if args.command in ('release', 'ack-delivery', 'delivery-unknown'):
        session = human()
        with RunLock(data.root):
            ledger = data.ledger(args.tenant, data.allowlist(args.tenant), capture.now_utc)
            try:
                row = ledger.report(args.report)
                print(f'Report {row["report_id"]} for {args.tenant}: state {row["state"]}\nFile: {row["html_path"]}')
                if ask(f'Type the report id ({args.report}) to confirm: ') != args.report:
                    print('Not confirmed; nothing changed.')
                    return EXIT_ERROR
                if args.command == 'release':
                    ledger.release(session, args.report)
                    print('Released for sending. NOTHING WAS SENT. Send it yourself, then run ack-delivery.')
                elif args.command == 'ack-delivery':
                    ledger.acknowledge_delivery(session, args.report, args.channel)
                    print('Delivery acknowledged; its changes will not be repeated in later reports.')
                else:
                    ledger.mark_delivery_unknown(session, args.report, 'operator unsure whether it was sent')
                    print('Marked unknown; later drafts will flag its items as possibly already sent.')
            finally:
                ledger.close()
        return EXIT_OK
    if args.command == 'import':
        session = human('manual_entry')
        allowlist = data.allowlist(args.tenant)
        facts = dict(item.split('=', 1) for item in args.field)
        record = capture.manual_record(allowlist, args.page, args.observed_at, facts, args.failed)
        with RunLock(data.root):
            ledger = data.ledger(args.tenant, allowlist, capture.now_utc)
            ledger.sync_sources(allowlist, capture.access_attested)
            print('recorded' if ledger.ingest_manual(record, session)[1] else 'already recorded')
            ledger.close()
        return EXIT_OK
    if args.command == 'add-source':
        path = data.tenant_dir(args.tenant) / 'allowlist.json'
        allowlist = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
            'tenant_id': args.tenant, 'market': args.market, 'synthetic': False, 'pages': []}
        if args.contact:
            allowlist['user_agent'] = f'OfferWatchBot/0.2 (+{args.contact})'
        fields = {}
        for item in args.field:
            name, pattern = item.split('=', 1)
            fields[name] = {'pattern': pattern, 'case': 'insensitive' if name in args.case_insensitive else 'sensitive'}
        entry = {'id': args.id, 'url': args.url, 'approved_by': args.approved_by, 'approved_at': args.approved_at,
                 'fields': fields, 'access_rules_checked_by': '', 'access_rules_checked_at': ''}
        allowlist['pages'] = [p for p in allowlist['pages'] if p['id'] != args.id] + [entry]
        data.save_allowlist(allowlist)
        print(f'Saved {args.id} for {args.tenant}. No request will be made until: attest-access --tenant {args.tenant} --id "{args.id}"')
        return EXIT_OK
    if args.command == 'attest-access':
        session = human()
        allowlist = data.allowlist(args.tenant)
        entry = capture.entry_for(allowlist, args.id)
        print(f'Page: {entry["url"]}\nConfirm you have read this site\'s terms and the page is public: no login, '
              'no CAPTCHA, no paywall, and automated checks twice a week are acceptable.')
        if ask('Type YES to record your attestation: ') != 'YES':
            print('Not recorded.')
            return EXIT_ERROR
        entry.update(access_rules_checked_by=session.reviewer, access_rules_checked_at=iso(capture.now_utc()))
        data.save_allowlist(allowlist)
        print('Recorded.')
        return EXIT_OK
    if args.command == 'backup':
        print('Backup written to', backup(data, args.dest))
        return EXIT_OK
    if args.command == 'restore':
        aside = restore(data, args.source)
        print('Restored.' + (f' Previous data kept at {aside}' if aside else ''))
        return EXIT_OK
    if args.command == 'prune':
        policy = data.retention()
        print(f'Retention policy in use: {policy} (ledger rows are never pruned)')
        with RunLock(data.root):
            for tenant in data.tenants():
                ledger = data.ledger(tenant, data.allowlist(tenant), capture.now_utc)
                plan = ledger.prune(policy, apply=args.apply)
                print(f'{tenant}: {"deleted" if args.apply else "would delete (dry run; add --apply)"} {len(plan)} files')
                ledger.close()
        return EXIT_OK
    return EXIT_ERROR


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, PermissionError) as exc:
        sys.exit('refused: ' + str(exc))
