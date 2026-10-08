"""Pipeline, scheduling, status, Windows task definition, backup/restore and CLI gates."""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import capture
import offerwatch
import rehearsal

NS = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}


class Clock:
    def __init__(self, text):
        self.set(text)

    def set(self, text):
        self.now = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class Web(rehearsal.FakeWeb):
    pass


class PipelineCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = offerwatch.DataDir(self.root / 'data')
        for tenant in rehearsal.OWN:
            self.data.save_allowlist(rehearsal.allowlist(tenant))
        self.clock, self.web = Clock('2026-10-05T09:15:00'), Web()

    def run_once(self, **kw):
        return offerwatch.run_pipeline(self.data, transport=self.web, clock=self.clock, sleep=lambda s: None, **kw)


class DataDirTests(unittest.TestCase):
    def test_refuses_git_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.git').mkdir()
            with self.assertRaisesRegex(ValueError, 'git working tree'):
                offerwatch.DataDir(Path(tmp) / 'nested' / 'data')

    def test_repository_itself_refused(self):
        with self.assertRaises(ValueError):
            offerwatch.DataDir(Path(__file__).resolve().parent / 'data')

    def test_folder_and_allowlist_tenant_must_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = offerwatch.DataDir(tmp)
            data.save_allowlist(rehearsal.allowlist('alpha-agency'))
            os.rename(data.tenant_dir('alpha-agency'), data.tenant_dir('other-agency'))
            with self.assertRaisesRegex(ValueError, 'names tenant'):
                data.allowlist('other-agency')


class LockTests(unittest.TestCase):
    def test_overlap_prevented_and_stale_lock_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            with offerwatch.RunLock(tmp):
                with self.assertRaises(offerwatch.Locked):
                    with offerwatch.RunLock(tmp):
                        pass
            self.assertFalse((Path(tmp) / 'run.lock').exists())
            (Path(tmp) / 'run.lock').write_text('left by killed run')
            later = time.time() + offerwatch.LOCK_STALE_SECONDS + 5
            with offerwatch.RunLock(tmp, clock=lambda: later) as lock:
                self.assertTrue(lock.recovered.exists())   # set aside, not deleted


class SchedulingTests(PipelineCase):
    def test_two_slots_per_week(self):
        self.assertEqual(offerwatch.slot_start(datetime(2026, 10, 7, 23, tzinfo=timezone.utc)).day, 5)
        self.assertEqual(offerwatch.slot_start(datetime(2026, 10, 8, 1, tzinfo=timezone.utc)).day, 8)
        self.assertEqual(offerwatch.slot_start(datetime(2026, 10, 11, 23, tzinfo=timezone.utc)).day, 8)

    def test_missed_run_catches_up_and_repeat_is_quiet(self):
        self.run_once()
        first = len(self.web.requests)
        self.clock.set('2026-10-05T20:00:00')
        self.run_once()
        self.assertEqual(len(self.web.requests), first)        # same slot: nothing due
        self.clock.set('2026-10-10T18:00:00')                  # laptop was off Thu-Fri: Saturday run catches up
        self.run_once()
        self.assertGreater(len(self.web.requests), first)

    def test_retries_are_bounded(self):
        self.web.status[rehearsal.SHARED['Shared Air Partners']] = 503
        summary = self.run_once()
        failures = [f for f in summary['failures'] if f['page'] == 'Shared Air Partners']
        self.assertEqual([f['attempts'] for f in failures], [2, 2])   # each tenant: 2 attempts in-run
        page_hits = lambda: self.web.requests.count(rehearsal.SHARED['Shared Air Partners'])
        for hour in ('09:45', '10:30', '11:30', '13:30', '16:00', '20:00'):
            self.clock.set('2026-10-05T' + hour + ':00')
            self.run_once()
        self.assertEqual(page_hits(), 2 * offerwatch.FAILED_ATTEMPTS_PER_SLOT)  # per tenant, per slot

    def test_non_retryable_failures_are_not_retried(self):
        self.web.status[rehearsal.SHARED['Shared Air Partners']] = 404
        summary = self.run_once()
        self.assertEqual({f['attempts'] for f in summary['failures']}, {1})


class StatusTests(PipelineCase):
    def test_status_file_shows_failures_and_last_success(self):
        ok = self.run_once()
        self.assertEqual(ok['outcome'], 'OK')
        status = json.loads((self.data.root / 'status.json').read_text())
        self.assertEqual(status['last_success_at'], ok['finished_at'])
        self.clock.set('2026-10-08T09:15:00')
        self.web.status[rehearsal.SHARED['Shared Cooling Co']] = 500
        bad = self.run_once()
        text = (self.data.root / 'STATUS.txt').read_text()
        self.assertEqual(bad['outcome'], 'COMPLETED WITH FAILURES')
        self.assertIn('FAILURES NEEDING ATTENTION', text)
        self.assertIn('Shared Cooling Co: http_500', text)
        self.assertIn('Last fully successful run: ' + ok['finished_at'], text)
        self.assertIn('Nothing was sent', text)

    def test_one_tenant_error_does_not_stop_others(self):
        path = self.data.tenant_dir('alpha-agency') / 'allowlist.json'
        path.write_text('{"broken": true}')
        summary = self.run_once()
        self.assertTrue(any(e.startswith('alpha-agency') for e in summary['errors']))
        self.assertEqual(summary['tenants']['beta-agency']['captured'], 5)

    def test_logs_and_status_contain_no_page_text(self):
        self.run_once()
        blob = (self.data.root / 'STATUS.txt').read_text() + (self.data.root / 'logs' / 'runs.log').read_text()
        self.assertNotIn('tune-up', blob)
        self.assertNotIn('APR', blob)

    def test_synthetic_tenant_never_uses_real_network(self):
        with mock.patch.object(capture, 'resolve_public', side_effect=AssertionError('network')):
            summary = offerwatch.run_pipeline(self.data, clock=self.clock, sleep=lambda s: None)
        self.assertEqual(summary['requests'], 0)
        self.assertTrue(all('Synthetic tenant' in e for e in summary['errors']))


class TaskXmlTests(unittest.TestCase):
    def test_definition_is_disabled_and_safe(self):
        xml = offerwatch.task_xml(r'C:\Python312\pythonw.exe', r'C:\Users\Ray\OfferWatch & Co\prototype\offerwatch.py')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'task.xml'
            path.write_text(xml, encoding='utf-16')
            self.assertEqual(path.read_bytes()[:2], b'\xff\xfe')
            root = ET.parse(path).getroot()
        settings = root.find('t:Settings', NS)
        get = lambda tag: settings.find('t:' + tag, NS).text
        self.assertEqual(get('Enabled'), 'false')
        self.assertEqual(get('MultipleInstancesPolicy'), 'IgnoreNew')
        self.assertEqual(get('StartWhenAvailable'), 'true')
        self.assertEqual(get('WakeToRun'), 'false')
        self.assertEqual(get('ExecutionTimeLimit'), 'PT1H')
        self.assertEqual(settings.find('t:RestartOnFailure/t:Count', NS).text, '2')
        self.assertEqual(root.find('t:Principals/t:Principal/t:RunLevel', NS).text, 'LeastPrivilege')
        args = root.find('t:Actions/t:Exec/t:Arguments', NS).text
        self.assertEqual(args, r'"C:\Users\Ray\OfferWatch & Co\prototype\offerwatch.py" run --scheduled')


class BackupTests(PipelineCase):
    def test_round_trip_tamper_detection_and_nothing_deleted(self):
        self.run_once()
        target = offerwatch.backup(self.data, self.root / 'backups')
        self.assertTrue(offerwatch.verify_backup(target)['files'])
        self.clock.set('2026-10-08T09:15:00')
        self.run_once()                                   # newer data that a restore would replace
        aside = offerwatch.restore(self.data, target)
        self.assertTrue((aside / 'tenants' / 'alpha-agency' / 'ledger.sqlite3').exists())
        led = self.data.ledger('alpha-agency', self.data.allowlist('alpha-agency'), self.clock)
        self.assertEqual(led._rows("SELECT COUNT(*) n FROM observations WHERE observed_at>='2026-10-08'")[0]['n'], 0)
        led.close()
        tampered = Path(target) / 'tenants' / 'alpha-agency' / 'allowlist.json'
        tampered.write_text(tampered.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'changed or damaged'):
            offerwatch.restore(self.data, target)

    def test_backup_refused_inside_git_tree(self):
        with self.assertRaises(ValueError):
            offerwatch.backup(self.data, Path(__file__).resolve().parent / 'backups')


class CliGateTests(PipelineCase):
    def call(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = offerwatch.main(['--data-dir', str(self.data.root)] + list(argv))
        return code, out.getvalue()

    def test_human_steps_refuse_without_terminal(self):
        self.run_once()
        for argv in (['release', '--tenant', 'alpha-agency', '--report', 'x'],
                     ['ack-delivery', '--tenant', 'alpha-agency', '--report', 'x', '--channel', 'email'],
                     ['review'], ['attest-access', '--tenant', 'alpha-agency', '--id', 'Alpha Duct Doctors'],
                     ['import', '--tenant', 'alpha-agency', '--page', 'Shared Cooling Co',
                      '--observed-at', '2026-10-05T09:00:00Z', '--field', 'advertised_offer=$1']):
            with self.subTest(argv=argv[0]), mock.patch('sys.stdin', io.StringIO('Ray\nYES\n')):
                with self.assertRaisesRegex(PermissionError, 'interactive terminal'):
                    self.call(*argv)
        allowlist = self.data.allowlist('alpha-agency')
        self.assertFalse(capture.access_attested(capture.entry_for(allowlist, 'Alpha Duct Doctors')))

    def test_add_source_writes_validated_unattested_entry(self):
        code, out = self.call('add-source', '--tenant', 'new-agency', '--contact', 'mailto:ops@example.invalid',
                              '--id', 'Rival HVAC', '--url', 'https://rival.example.com/offers',
                              '--approved-by', 'Client contact', '--approved-at', '2026-10-08T12:00:00Z',
                              '--field', r'advertised_offer=Special:\s*(.+?)\|', '--case-insensitive', 'financing')
        self.assertEqual(code, 0)
        entry = self.data.allowlist('new-agency')['pages'][0]
        self.assertFalse(capture.access_attested(entry))
        self.assertIn('attest-access', out)
        with self.assertRaises(ValueError):
            self.call('add-source', '--tenant', 'new-agency', '--id', 'Bad', '--url', 'http://insecure.example.com/',
                      '--approved-by', 'x', '--approved-at', '2026-10-08T12:00:00Z', '--field', 'a=(b)')

    def test_task_xml_command_installs_nothing(self):
        out = self.root / 'task.xml'
        code, text = self.call('task-xml', '--out', str(out), '--python', r'C:\Python\pythonw.exe')
        self.assertEqual(code, 0)
        self.assertIn('DISABLED', text)
        self.assertIn('<Enabled>false</Enabled>', out.read_text(encoding='utf-16'))


class RehearsalTests(unittest.TestCase):
    def test_offline_rehearsal_passes_all_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = rehearsal.run_rehearsal(Path(tmp) / 'data', Path(tmp) / 'out')
            self.assertEqual(result['failed_checks'], [])
            self.assertTrue((Path(tmp) / 'out' / 'sample_draft_alpha-agency.html').exists())
            with self.assertRaises(ValueError):           # never wipes an existing folder
                rehearsal.run_rehearsal(Path(tmp) / 'data', Path(tmp) / 'out')


if __name__ == '__main__':
    unittest.main()
