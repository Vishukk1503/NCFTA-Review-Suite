"""The setup uplink check uses synthetic metadata only; never live keys."""
import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

import requests
import check_connection as check
import connection_doctor as doctor
import suite


class SetupConnectionTests(unittest.TestCase):
    cfg = {'url': 'https://misp.example.invalid', 'env': 'unused', 'transport': 'requests'}

    def worker_response(self, status=200, payload=None, error=None, raw=None):
        response = Mock(status_code=status)
        response.headers = {'Content-Type': 'application/json'}
        response.request = requests.Request('GET', self.cfg['url'],
                                            headers={'Authorization': 'SYNTHETIC_KEY'}).prepare()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [raw if raw is not None else json.dumps(payload).encode()]
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.request.return_value = response
        session.request.side_effect = error
        conn = Mock()
        with patch.object(check, 'configure_tls', return_value=(True, 'Windows certificate store')), \
             patch.object(requests, 'Session', return_value=session):
            check.quick_worker(conn, 'auth', dict(self.cfg, key='SYNTHETIC_KEY'))
        row = conn.send.call_args.args[0]
        self.assertNotIn('SYNTHETIC_KEY', json.dumps(row))
        self.assertNotIn('PRIVATE_USER', json.dumps(row))
        conn.close.assert_called_once()
        call = session.request.call_args
        self.assertEqual(session.request.call_count, 1)
        self.assertEqual(call.args, ('GET', self.cfg['url'] + '/users/view/me'))
        self.assertIs(call.kwargs['verify'], True)
        self.assertIs(call.kwargs['allow_redirects'], False)
        self.assertNotIn('json', call.kwargs)
        return row

    def test_one_authenticated_get_discards_account_details(self):
        row = self.worker_response(payload={'User': {'email': 'PRIVATE_USER'}})
        self.assertEqual(row['code'], 'PASS')
        self.assertEqual(row['http_status'], 200)
        self.assertTrue(row['tls_verified'])

    def test_non_json_denial_and_timeout_are_not_success(self):
        self.assertEqual(self.worker_response(raw=b'<html>PRIVATE_USER</html>')['code'], 'NOT_JSON')
        self.assertEqual(self.worker_response(status=403, payload={'error': 'PRIVATE_USER'})['code'], 'HTTP_403')
        self.assertEqual(self.worker_response(error=requests.exceptions.ReadTimeout('SYNTHETIC_KEY PRIVATE_USER'))['code'], 'READ_TIMEOUT')

    def test_quick_check_uses_bounded_worker_and_configured_key(self):
        with patch.object(suite, 'credentials', return_value='SYNTHETIC_KEY'), \
             patch.object(doctor, 'run_step', return_value={'code': 'PASS', 'seconds': 0.8}) as step:
            row = check.quick_check(dict(self.cfg, ca_bundle='trusted.pem'))
        self.assertTrue(row['ok'])
        self.assertEqual(step.call_args.kwargs, {'seconds': 20, 'target': check.quick_worker, 'quiet': True})
        self.assertEqual(step.call_args.args[2]['key'], 'SYNTHETIC_KEY')
        self.assertEqual(step.call_args.args[2]['ca_bundle'], 'trusted.pem')
        self.assertNotIn('SYNTHETIC_KEY', json.dumps(row))

    def test_missing_bad_key_and_invalid_origin_do_not_request(self):
        with patch.object(doctor, 'run_step') as step:
            for key in ('YOUR_ACTUAL_KEY', 'BAD KEY', 'BAD\x00KEY'):
                with patch.object(suite, 'credentials', return_value=key):
                    self.assertFalse(check.quick_check(self.cfg)['ok'])
            with patch.object(suite, 'credentials', side_effect=suite.SuiteError('No API key found.')):
                self.assertEqual(check.quick_check(self.cfg)['code'], 'CONFIG')
            with patch.object(suite, 'credentials') as credentials:
                self.assertFalse(check.quick_check(dict(self.cfg, url='http://misp.example.invalid'))['ok'])
                credentials.assert_not_called()
            step.assert_not_called()

    def test_tls_config_failure_does_not_send_request(self):
        conn = Mock()
        with patch.object(check, 'configure_tls', side_effect=check.TLSConfigurationError('Invalid CA bundle.')), \
             patch.object(doctor, 'http_probe') as probe:
            check.quick_worker(conn, 'auth', dict(self.cfg, key='SYNTHETIC_KEY'))
        probe.assert_not_called()
        self.assertEqual(conn.send.call_args.args[0]['code'], 'TLS_CONFIG')

    def test_deadline_is_visible_as_failure(self):
        with patch.object(suite, 'credentials', return_value='SYNTHETIC_KEY'), \
             patch.object(doctor, 'run_step', return_value={'code': 'DEADLINE', 'message': 'Step timed out.', 'seconds': 20}):
            row = check.quick_check(self.cfg)
        self.assertFalse(row['ok'])
        from rich.console import Console
        output = io.StringIO()
        Console(file=output, width=100).print(suite.connection_panel(row))
        self.assertIn('CHECK FAILED', output.getvalue())
        self.assertIn('Not confirmed', output.getvalue())
        self.assertIn('20.00 seconds', output.getvalue())

    def test_menu_action_preserves_connection_exit_code(self):
        with patch.object(suite, 'settings', return_value=self.cfg), patch.object(suite, 'doctor', return_value=1):
            self.assertEqual(suite.main(['doctor']), 1)
        with patch('rich.prompt.Prompt.ask', return_value='5'), patch('rich.console.Console.print'):
            self.assertEqual(suite.interactive(self.cfg), ['doctor'])


if __name__ == '__main__':
    unittest.main()
