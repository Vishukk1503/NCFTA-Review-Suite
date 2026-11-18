"""Synthetic connection checks; never use real credentials or event data."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests
import connection_doctor as doctor

SECRET = 'SECRET_TEST_KEY'
PRIVATE = 'PRIVATE_EVENT_DATA'


def hanging_worker(conn, kind, cfg):
    time.sleep(10)


class DoctorTests(unittest.TestCase):
    def cfg(self):
        return dict(url='https://misp.example.invalid', host='misp.example.invalid', port=443,
                    key=SECRET, ca_bundle=None, group=6, start='2026-11-15', end='2026-11-15')

    def test_settings_matches_suite_key_precedence_and_quotes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / 'settings.json').write_text(json.dumps({'url': self.cfg()['url'], 'env': 'elsewhere.env', 'transport': 'requests'}))
            (root / 'elsewhere.env').write_text('NCFTA_API_KEY=wrong')
            (root / '.env').write_text('export NCFTA_API_KEY = "' + SECRET + '"\n', encoding='utf-8-sig')
            cfg = doctor.configuration(root)
            self.assertEqual(cfg['key'], SECRET)
            self.assertFalse(cfg['key_issue'])
            with patch.dict(os.environ, NCFTA_API_KEY='ENV_KEY'):
                self.assertEqual(doctor.configuration(root)['key'], 'ENV_KEY')
            for url in ('http://example.invalid', 'https://a:b@example.invalid', 'https://example.invalid/path', 'https://example.invalid?key=' + SECRET):
                (root / 'settings.json').write_text(json.dumps({'url': url}))
                with self.assertRaises(doctor.ConfigError) as caught:
                    doctor.configuration(root)
                self.assertNotIn(SECRET, str(caught.exception))

    def probe(self, kind, status=200, payload=None, error=None):
        response = Mock(status_code=status)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [json.dumps(payload).encode()]
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.request.return_value = response
        session.request.side_effect = error
        with patch.object(requests, 'Session', return_value=session):
            row = doctor.http_probe(self.cfg(), kind)
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertNotIn(PRIVATE, json.dumps(row))
        call = session.request.call_args
        self.assertTrue(call.kwargs['verify'])
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertEqual(call.kwargs['timeout'], (5, 15))
        self.assertEqual(session.request.call_count, 1)
        return row, call, response

    def test_login_never_sends_key_and_does_not_follow_redirect(self):
        row, call, response = self.probe('login', status=302)
        self.assertEqual(row['code'], 'PASS')
        self.assertNotIn('Authorization', call.kwargs['headers'])
        response.iter_content.assert_not_called()

    def test_metadata_success_and_auth_is_independent(self):
        for kind, payload in [('version', {'version': 'test'}), ('auth', {'User': {'email': PRIVATE}}),
                              ('types', {'result': {'types': ['other']}})]:
            row, call, _ = self.probe(kind, payload=payload)
            self.assertEqual(row['code'], 'PASS')
            self.assertEqual(call.kwargs['headers']['Authorization'], SECRET)
        row, _, _ = self.probe('auth', payload={'version': 'test'})
        self.assertEqual(row['code'], 'SHAPE')

    def test_http_failures_body_never_read(self):
        for status in (302, 401, 403, 404, 407, 429, 500, 502, 503):
            row, _, response = self.probe('auth', status, payload={PRIVATE: SECRET})
            self.assertEqual(row['code'], f'HTTP_{status}')
            response.iter_content.assert_not_called()

    def test_network_failures_are_distinct_and_safe(self):
        for kind, expected in [(requests.exceptions.SSLError, 'TLS'), (requests.exceptions.ProxyError, 'PROXY'),
                               (requests.exceptions.ConnectTimeout, 'CONNECT_TIMEOUT'), (requests.exceptions.ReadTimeout, 'READ_TIMEOUT'),
                               (requests.exceptions.ConnectionError, 'CONNECTION'), (requests.exceptions.InvalidHeader, 'KEY_FORMAT')]:
            row, _, _ = self.probe('auth', error=kind(PRIVATE + SECRET))
            self.assertEqual(row['code'], expected)

    def test_event_search_is_one_row_scoped_read_only(self):
        payload = {'response': [{'Event': {'sharing_group_id': '6', 'date': '2026-11-15', 'Attribute': [{'value': PRIVATE}]}}]}
        row, call, _ = self.probe('events', payload=payload)
        self.assertEqual(row['code'], 'PASS')
        self.assertEqual(row['events'], 1)
        self.assertEqual(call.args, ('POST', 'https://misp.example.invalid/events/restSearch'))
        self.assertEqual(call.kwargs['json'], {'returnFormat': 'json', 'sharinggroup': [6], 'from': '2026-11-15',
                                             'to': '2026-11-15', 'page': 1, 'limit': 1, 'metadata': False, 'withAttachments': False})
        row, _, _ = self.probe('events', payload=[])
        self.assertEqual(row['code'], 'EMPTY')
        self.assertIn('do not prove', row['message'])
        payload['response'][0]['Event']['sharing_group_id'] = '8'
        row, _, _ = self.probe('events', payload=payload)
        self.assertEqual(row['code'], 'SCOPE')
        row, _, _ = self.probe('events', payload={'errors': PRIVATE})
        self.assertEqual(row['code'], 'API_ERROR')

    def test_hard_deadline_terminates_stalled_process(self):
        start = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            row = doctor.run_step('auth', 'synthetic hang', self.cfg(), seconds=1, target=hanging_worker)
        self.assertEqual(row['code'], 'DEADLINE')
        self.assertLess(time.monotonic() - start, 5)

    def test_main_continues_after_metadata_failure_and_saves_no_secrets(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / 'settings.json').write_text(json.dumps({'url': self.cfg()['url']}))
            (root / '.env').write_text('NCFTA_API_KEY=' + SECRET)
            calls = []
            def step(kind, label, cfg, seconds=25):
                calls.append(kind)
                return dict(step=kind, label=label, code='READ_TIMEOUT' if kind == 'version' else 'PASS', message='synthetic')
            output = io.StringIO()
            with patch.object(doctor, 'run_step', step), contextlib.redirect_stdout(output):
                code = doctor.main(['--suite-dir', str(root), '--from-date', '11/15/2026', '--to-date', '11/15/2026', '--groups', 'all'])
            self.assertEqual(code, 1)
            self.assertEqual(calls, ['proxy', 'dns', 'tcp', 'login', 'version', 'auth', 'types', 'events', 'events', 'events'])
            report_text = (root / 'setup' / 'connection-doctor.json').read_text()
            self.assertNotIn(SECRET, report_text + output.getvalue())
            self.assertEqual(json.loads(report_text)['event_requests'], 3)

    def test_bat_spaces_metacharacters_and_exit_code(self):
        if os.name != 'nt':
            self.skipTest('Windows launcher')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'suite & doctor'
            root.mkdir()
            shutil.copyfile(doctor.HERE / 'Connection-Doctor.bat', root / 'Connection-Doctor.bat')
            for exit_code in (0, 7):
                (root / 'connection_doctor.py').write_text(f'import sys; print("ARGS="+str(sys.argv[1:])); sys.exit({exit_code})')
                command = f'"{os.environ["COMSPEC"]}" /d /c call "{root / "Connection-Doctor.bat"}" --metadata-only'
                run = subprocess.run(command, env=dict(os.environ, NCFTA_PYTHON=sys.executable), input='',
                                     text=True, capture_output=True, timeout=15)
                self.assertEqual(run.returncode, exit_code, run.stdout + run.stderr)
                self.assertIn('--metadata-only', run.stdout)
                self.assertEqual(run.stdout.count('Press any key to CLOSE'), 1)


if __name__ == '__main__':
    unittest.main()
