"""RCA integration and safety tests: synthetic only, no real credentials/data."""
import contextlib
import ctypes as C
import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
import connection_doctor as doctor
import connection_rca as rca
import connection_windows as native

SECRET = 'TEST_SECRET'
PRIVATE = 'PRIVATE_EVENT_VALUE'


class RCATests(unittest.TestCase):
    def cfg(self, root=None):
        return dict(url='https://misp.example.invalid', host='misp.example.invalid', port=443,
                    key=SECRET, ca_bundle=None, group=6, start='2026-11-11', end='2026-11-14',
                    suite_root=str(root), event_variant='suite')

    def suite_fixture(self, root):
        for name in ('suite.py', 'tls_support.py'):
            shutil.copyfile(doctor.HERE / name, root / name)
        (root / '.env').write_text('NCFTA_API_KEY=' + SECRET)
        (root / 'settings.json').write_text(json.dumps(dict(url=self.cfg()['url'], env='.env', runs='runs', transport='requests')))

    def response(self, request, payload, status=200, content_type='application/json'):
        response = requests.Response()
        response.status_code = status
        response.headers['Content-Type'] = content_type
        response._content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        response._content_consumed = True
        response.request = request
        return response

    def test_installed_network_definitions_fetch_without_model_imports(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            self.suite_fixture(root)
            with (root / 'suite.py').open('a') as stream:
                stream.write('\nraise RuntimeError("TOP LEVEL MUST NOT EXECUTE")\n')
            calls = []
            def send(session, request, **kwargs):
                calls.append((request, kwargs))
                self.assertEqual(json.loads(request.body)['limit'], 1)
                return self.response(request, [{'Event': {'sharing_group_id': '6', 'date': '2026-11-11',
                                                           'Attribute': [{'value': PRIVATE}]}}])
            with patch.object(requests.Session, 'send', send):
                row = rca.suite_event_probe(self.cfg(root))
            self.assertEqual(row['code'], 'PASS', row)
            self.assertEqual(row['attributes'], 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1]['timeout'], (5, 60))
            self.assertFalse(calls[0][1]['allow_redirects'])
            self.assertTrue(calls[0][1]['verify'])
            self.assertNotIn(SECRET, json.dumps(row))
            self.assertNotIn(PRIVATE, json.dumps(row))

    def test_installed_suite_failure_retains_safe_network_cause(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            self.suite_fixture(root)
            with patch.object(requests.Session, 'send', side_effect=requests.exceptions.SSLError(SECRET + PRIVATE)):
                row = rca.suite_event_probe(self.cfg(root))
            self.assertEqual(row['code'], 'TLS', row)
            self.assertNotIn(SECRET, json.dumps(row))
            self.assertNotIn(PRIVATE, json.dumps(row))

    def test_installed_suite_403_and_html_keep_status_without_private_content(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            self.suite_fixture(root)
            for status, content_type, expected in ((403, 'text/html', 'HTTP_403'), (200, 'text/html', 'NOT_JSON')):
                def send(session, request, **kwargs):
                    return self.response(request, ('<html>Access denied ' + PRIVATE + SECRET + '</html>').encode(), status, content_type)
                with patch.object(requests.Session, 'send', send):
                    row = rca.suite_event_probe(self.cfg(root))
                self.assertEqual(row['code'], expected)
                self.assertNotIn(PRIVATE, json.dumps(row))
                self.assertNotIn(SECRET, json.dumps(row))
                self.assertIn('access_block_message', row['page_markers'])

    def test_installed_client_cannot_change_scope_key_origin_or_disable_tls(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            cases = [
                ("'limit':args['limit']", "'limit':10", 'REQUEST_MISMATCH'),
                ("self.url=url+'/events/restSearch'", "self.url='https://other.example.invalid/events/restSearch'", 'REFUSED'),
                ('verify=self.verify', 'verify=False', 'TLS_DISABLED'),
                ('Authorization=key', "Authorization='different'", 'KEY_REPLACED'),
            ]
            for old, new, expected in cases:
                self.suite_fixture(root)
                path = root / 'suite.py'
                path.write_text(path.read_text(encoding='utf-8').replace(old, new), encoding='utf-8')
                with patch.object(requests.Session, 'send') as send:
                    row = rca.suite_event_probe(self.cfg(root))
                self.assertEqual(row['code'], expected, row)
                send.assert_not_called()
            self.suite_fixture(root)
            (root / '.env').write_text('NCFTA_API_KEY=different')
            with patch.object(requests.Session, 'send') as send:
                self.assertEqual(rca.suite_event_probe(self.cfg(root))['code'], 'CONFIG_MISMATCH')
            send.assert_not_called()

    def test_windows_post_keeps_scope_body_and_does_not_follow_redirect(self):
        calls = []
        def fake_bind(lib, name, restype, args):
            def fn(*values):
                calls.append((name, values))
                if name == 'WinHttpQueryHeaders':
                    if values[1] == 19 | 0x20000000:
                        C.cast(values[3], C.POINTER(native.D)).contents.value = 302
                        return 1
                    return 0
                if name == 'WinHttpQueryOption':
                    return 0
                if name == 'WinHttpReadData':
                    C.cast(values[3], C.POINTER(native.D)).contents.value = 0
                if name == 'WinHttpSendRequest':
                    self.assertEqual(values[4], values[5])
                    body = json.loads(C.string_at(values[3], values[4]))
                    self.assertEqual(body, doctor.event_body(self.cfg()))
                if name == 'WinHttpSetOption' and values[1] == 63:
                    self.assertEqual(C.cast(values[2], C.POINTER(native.D)).contents.value, 7)
                return 1
            return fn
        with patch.object(native, 'dll', return_value=object()), patch.object(native, 'bind', fake_bind):
            cfg = self.cfg()
            row = native.winhttp_request(cfg, '/events/restSearch', doctor.event_body(cfg))
        self.assertEqual(row['code'], 'HTTP_302')
        self.assertEqual(next(v for n, v in calls if n == 'WinHttpOpenRequest')[1], 'POST')
        self.assertEqual(sum(n == 'WinHttpSendRequest' for n, _ in calls), 1)
        self.assertEqual(sum(n == 'WinHttpCloseHandle' for n, _ in calls), 3)
        bad = doctor.event_body(cfg)
        bad['limit'] = 10
        self.assertEqual(native.winhttp_request(cfg, '/events/restSearch', bad)['code'], 'REFUSED')
        self.assertEqual(native.winhttp_request(cfg, '/events/add', {})['code'], 'REFUSED')

    def test_followups_are_bounded_and_denials_do_not_trigger_format_retries(self):
        self.assertEqual(rca.followup_variants({'code': 'NOT_JSON'}), ['json_endpoint', 'identity', 'metadata'])
        self.assertEqual(rca.followup_variants({'code': 'READ_TIMEOUT'}), ['metadata', 'long_timeout'])
        for code in ('PASS', 'EMPTY', 'HTTP_401', 'HTTP_403', 'HTTP_429', 'TLS', 'BUDGET'):
            self.assertEqual(rca.followup_variants({'code': code}), [])

    def test_variants_change_only_one_request_property(self):
        for variant in ('json_endpoint', 'identity', 'metadata', 'long_timeout'):
            cfg = dict(self.cfg(), event_variant=variant)
            response = self.response(requests.Request('POST', cfg['url'], headers={'Authorization': SECRET}).prepare(), [])
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            session = Mock()
            session.__enter__ = Mock(return_value=session)
            session.__exit__ = Mock(return_value=False)
            session.request.return_value = response
            with patch.object(requests, 'Session', return_value=session):
                row = doctor.http_probe(cfg, 'events')
            self.assertEqual(row['code'], 'EMPTY')
            call = session.request.call_args
            self.assertEqual(call.kwargs['json']['sharinggroup'], [6])
            self.assertEqual(call.kwargs['json']['limit'], 1)
            self.assertFalse(call.kwargs['json']['withAttachments'])
            self.assertEqual(call.kwargs['json']['metadata'], variant == 'metadata')
            self.assertEqual(call.args[1].endswith('.json'), variant == 'json_endpoint')
            self.assertEqual(call.kwargs['headers'].get('Accept-Encoding'), 'identity' if variant == 'identity' else None)
            self.assertEqual(call.kwargs['timeout'], (5, 60 if variant == 'long_timeout' else 15))
            self.assertEqual(session.request.call_count, 1)

    def test_report_does_not_turn_sequential_difference_into_proven_vpn_cause(self):
        def event(variant, code):
            return dict(step='events', variant=variant, group=6, code=code, label=variant)
        report = dict(checks=[event('python', 'NOT_JSON'), event('native', 'PASS'), event('suite', 'NOT_JSON'), event('identity', 'PASS')],
                      filters=dict(groups=[6]))
        diagnosis = rca.analyse(report)
        self.assertEqual(diagnosis['status'], 'FAILURE_OR_INCOMPLETE')
        codes = {f['code'] for f in diagnosis['findings']}
        self.assertIn('GROUP_6_WINDOWS_PATH_WORKS', codes)
        self.assertIn('GROUP_6_IDENTITY_WORKS', codes)
        self.assertIn('transient', json.dumps(diagnosis))
        report['checks'] = [event(v, 'EMPTY') for v in ('python', 'native', 'suite')]
        diagnosis = rca.analyse(report)
        self.assertEqual(diagnosis['status'], 'SCOPED_FETCH_WORKS')
        self.assertIn('GROUP_6_EMPTY', {f['code'] for f in diagnosis['findings']})

    def test_inventory_reads_only_run_status_and_sanitizes_log_values(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            self.suite_fixture(root)
            folder = root / 'runs' / 'private-name'
            folder.mkdir(parents=True)
            (folder / 'manifest.json').write_text(json.dumps(dict(fetch_complete=False, value=PRIVATE, key=SECRET)))
            (folder / 'run.log').write_text(json.dumps(dict(code='fetch_retry', group=6, value=PRIVATE, key=SECRET)) + '\n')
            row = rca.inventory(self.cfg(root))
            self.assertEqual(row['recent_run_states'][0]['fetch_retries_in_last_64kb']['6'], 1)
            for value in (PRIVATE, SECRET, 'private-name'):
                self.assertNotIn(value, json.dumps(row))

    def test_correlation_headers_are_allowlisted_not_arbitrary_text(self):
        row = rca.correlation_facts({'CF-Ray': 'abcdef0123456789-IAD', 'X-Request-ID': SECRET,
                                     'Set-Cookie': PRIVATE, 'Date': 'bad date', 'Retry-After': '30'})
        self.assertEqual(row['correlation_ids'], {'CF-Ray': 'abcdef0123456789-IAD'})
        self.assertEqual(row['retry_after_seconds'], 30)
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertNotIn(PRIVATE, json.dumps(row))

    def test_denied_page_markers_are_bounded_and_anonymous_login_is_not_api_failure(self):
        from test_connection_doctor import DoctorTests
        helper = DoctorTests()
        row, _, response = helper.probe('auth', status=403,
            raw=('<html>Access denied ' + PRIVATE + SECRET + 'x' * 100000 + '</html>').encode(),
            headers={'Content-Type': 'text/html'})
        self.assertEqual(row['code'], 'HTTP_403')
        self.assertEqual(row['response_bytes'], 65536)
        self.assertTrue(row['body_sample_truncated'])
        self.assertIn('access_block_message', row['page_markers'])
        self.assertNotIn(PRIVATE, json.dumps(row))
        self.assertNotIn(SECRET, json.dumps(row))
        rows = [dict(step='login', label='login', code='PASS', page_markers=['misp_login_form']),
                dict(step='auth', label='auth', code='PASS')]
        self.assertNotIn('API response contains a MISP login', ' '.join(doctor.diagnosis(rows)))

    def test_overall_budget_records_unattempted_checks_without_network_calls(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            self.suite_fixture(root)
            with patch.object(doctor.time, 'monotonic', side_effect=[0, 0] + [700] * 100), patch.object(doctor, 'run_step') as step, contextlib.redirect_stdout(io.StringIO()):
                code = doctor.main(['--suite-dir', str(root), '--from-date', '2026-11-11', '--to-date', '2026-11-14'])
            self.assertEqual(code, 1)
            step.assert_not_called()
            report = json.loads((root / 'setup' / 'connection-doctor.json').read_text())
            self.assertIn('DIAGNOSTIC_DEADLINE', {r['code'] for r in report['rca']['findings']})
            self.assertTrue((root / 'setup' / 'connection-rca.txt').is_file())


if __name__ == '__main__':
    unittest.main()
