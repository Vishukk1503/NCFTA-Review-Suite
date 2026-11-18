"""Connection regression checks using synthetic responses, never a real key."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from pymisp.exceptions import PyMISPError
import check_connection as check
import ncfta_client
import suite


class ConnectionTests(unittest.TestCase):
    def response(self, status=200, payload=None):
        response = Mock(status_code=status)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.json.return_value = payload
        return response

    def run_probe(self, side_effect):
        session = Mock()
        session.get.side_effect = side_effect
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rows = check.probe('https://misp.example.invalid', 'SECRET_TEST_KEY', session, requests)
        self.assertNotIn('SECRET_TEST_KEY', output.getvalue() + json.dumps(rows))
        self.assertNotIn('PRIVATE_RESPONSE', output.getvalue() + json.dumps(rows))
        for call in session.get.call_args_list:
            self.assertEqual(call.kwargs['allow_redirects'], False)
            self.assertIs(call.kwargs['verify'], True)
            self.assertEqual(call.kwargs['timeout'], (5, 10))
        return rows, session

    def test_success_uses_only_three_metadata_gets(self):
        rows, session = self.run_probe([
            self.response(payload={'version': '2.5.test'}),
            self.response(payload={'User': {'email': 'PRIVATE_RESPONSE'}}),
            self.response(payload={'result': {'types': ['other']}}),
        ])
        self.assertTrue(all(row['ok'] for row in rows))
        self.assertEqual([call.args[0] for call in session.get.call_args_list],
                         ['https://misp.example.invalid' + p for p in check.ENDPOINTS])
        session.post.assert_not_called()

    def test_denied_redirect_and_server_failure_stop_without_body(self):
        for status in (301, 302, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                response = self.response(status, {'PRIVATE_RESPONSE': 'SECRET_TEST_KEY'})
                rows, session = self.run_probe([response])
                self.assertFalse(rows[0]['ok'])
                self.assertIn(str(status), rows[0]['message'])
                response.json.assert_not_called()
                self.assertEqual(session.get.call_count, 1)

    def test_success_status_with_wrong_body_is_not_pass(self):
        for payload in ({'error': 'PRIVATE_RESPONSE'}, {'version': 'x', 'errors': ['PRIVATE_RESPONSE']}, [], {'message': 'PRIVATE_RESPONSE'}):
            rows, _ = self.run_probe([self.response(payload=payload)])
            self.assertFalse(rows[0]['ok'])
        response = self.response()
        response.json.side_effect = ValueError('PRIVATE_RESPONSE')
        rows, _ = self.run_probe([response])
        self.assertIn('not JSON', rows[0]['message'])

    def test_network_failures_report_categories_without_raw_details(self):
        for kind, category in ((requests.exceptions.SSLError, 'TLS'),
                               (requests.exceptions.ProxyError, 'Proxy'),
                               (requests.exceptions.Timeout, 'timed out'),
                               (requests.exceptions.ConnectionError, 'Network'),
                               (requests.exceptions.InvalidHeader, 'header')):
            rows, _ = self.run_probe([kind('PRIVATE_RESPONSE SECRET_TEST_KEY')])
            self.assertIn(category, rows[0]['message'])

    def test_pymisp_wrapped_causes_are_recovered(self):
        for cause, expected in (
            (ncfta_client.NCFTAClientError('MISP request failed: HTTP 403.'), 'HTTP 403'),
            (requests.exceptions.SSLError('PRIVATE_RESPONSE SECRET_TEST_KEY'), 'TLS'),
            (requests.exceptions.ProxyError('PRIVATE_RESPONSE SECRET_TEST_KEY'), 'Proxy'),
            (requests.exceptions.Timeout('PRIVATE_RESPONSE SECRET_TEST_KEY'), 'timed out'),
        ):
            def constructor(*args, **kwargs):
                try:
                    raise cause
                except Exception:
                    raise PyMISPError('PRIVATE_RESPONSE SECRET_TEST_KEY')
            with patch.object(ncfta_client, 'PyMISP', constructor):
                with self.assertRaises(ncfta_client.NCFTAClientError) as caught:
                    with ncfta_client.connect('https://misp.example.invalid', 'SECRET_TEST_KEY', 5):
                        self.fail('Startup should fail.')
            message = suite.safe_failure_message(caught.exception)
            self.assertIn(expected, message)
            self.assertNotIn('PRIVATE_RESPONSE', message)
            self.assertNotIn('SECRET_TEST_KEY', message)

    def test_real_pymisp_wraps_http_failure_but_safe_reason_survives(self):
        response = requests.Response()
        response.status_code = 403
        response._content = b'PRIVATE_RESPONSE SECRET_TEST_KEY'
        response._content_consumed = True
        response.url = 'https://misp.example.invalid/servers/getVersion'
        with patch.object(requests.adapters.HTTPAdapter, 'send', return_value=response):
            with self.assertRaises(ncfta_client.NCFTAClientError) as caught:
                with ncfta_client.connect('https://misp.example.invalid', 'SECRET_TEST_KEY', 5):
                    self.fail('Startup should fail.')
        message = suite.safe_failure_message(caught.exception)
        self.assertIn('HTTP 403', message)
        self.assertNotIn('PRIVATE_RESPONSE', message)
        self.assertNotIn('SECRET_TEST_KEY', message)

    def test_unknown_errors_are_not_printed(self):
        message = suite.safe_failure_message(ValueError('PRIVATE_RESPONSE SECRET_TEST_KEY'))
        self.assertNotIn('PRIVATE_RESPONSE', message)
        self.assertNotIn('SECRET_TEST_KEY', message)

    def test_main_refuses_invalid_origin_or_key_before_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            for url, key in (('http://misp.example.invalid', 'SECRET_TEST_KEY'),
                             ('https://misp.example.invalid/path', 'SECRET_TEST_KEY'),
                             ('https://misp.example.invalid', 'SECRET TEST KEY'),
                             ('https://misp.example.invalid', 'YOUR_ACTUAL_KEY')):
                output = io.StringIO()
                with patch.object(check, 'HERE', Path(directory)), patch.object(suite, 'settings', return_value={'url': url}), \
                     patch.object(suite, 'credentials', return_value=key), patch.object(check, 'probe') as probe, \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(check.main(), 1)
                probe.assert_not_called()
                self.assertNotIn(key, output.getvalue())

    def test_main_report_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for ok, code in ((True, 0), (False, 1)):
                rows = [{'endpoint': p, 'ok': ok, 'message': 'synthetic'} for p in check.ENDPOINTS]
                with patch.object(check, 'HERE', root), patch.object(suite, 'settings', return_value={'url': 'https://misp.example.invalid'}), \
                     patch.object(suite, 'credentials', return_value='SECRET_TEST_KEY'), patch.object(check, 'probe', return_value=rows), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(check.main(), code)
                saved = json.loads((root / 'setup' / 'connection-report.json').read_text())
                self.assertEqual(saved['ok'], ok)
                self.assertEqual(saved['event_requests'], 0)
                self.assertFalse(saved['model_loaded'])
                self.assertNotIn('SECRET_TEST_KEY', json.dumps(saved))


@unittest.skipUnless(os.name == 'nt', 'Windows BAT')
class ConnectionLauncherTests(unittest.TestCase):
    def test_bat_stays_local_forwards_exit_and_handles_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'suite & connection check'
            root.mkdir()
            shutil.copyfile(Path(__file__).parent / 'Check-Connection.bat', root / 'Check-Connection.bat')
            (root / 'suite.py').write_text('# placeholder\n')
            for code in (0, 7):
                (root / 'check_connection.py').write_text(f"import os,sys; print('HERE='+os.getcwd()); sys.exit({code})\n")
                command = f'"{os.environ["COMSPEC"]}" /d /c call "{root / "Check-Connection.bat"}"'
                result = subprocess.run(command, cwd=directory, env=dict(os.environ, NCFTA_PYTHON=sys.executable),
                                        input='', capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn('HERE=' + str(root), result.stdout)
                self.assertEqual(result.stdout.count('Press any key to CLOSE this window.'), 1)
            (root / 'check_connection.py').unlink()
            result = subprocess.run(command, cwd=directory, env=dict(os.environ, NCFTA_PYTHON=sys.executable),
                                    input='', capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1)
            self.assertIn('check_connection.py is missing', result.stdout)


if __name__ == '__main__':
    unittest.main()
