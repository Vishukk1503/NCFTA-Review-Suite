"""TLS configuration tests. No real credentials or Windows trust-store writes."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import tls_support


class TLSTests(unittest.TestCase):
    def test_windows_trust_is_initialized_once_and_verification_stays_on(self):
        truststore = Mock()
        with patch.dict(os.environ, {}, clear=True), patch.object(tls_support.sys, 'platform', 'win32'), \
             patch.object(tls_support, '_windows_initialized', False), patch.dict('sys.modules', {'truststore': truststore}):
            self.assertEqual(tls_support.configure_tls(), (True, 'Windows certificate store'))
            self.assertEqual(tls_support.configure_tls(), (True, 'Windows certificate store'))
        truststore.inject_into_ssl.assert_called_once_with()

    def test_ca_bundle_precedence_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            files = [Path(directory) / f'{name}.pem' for name in ('configured', 'ncfta', 'requests', 'curl')]
            for file in files:
                file.write_text('synthetic path fixture')
            env = dict(zip(('NCFTA_CA_BUNDLE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE'), map(str, files[1:])))
            with patch.dict(os.environ, env, clear=True), patch.object(tls_support.sys, 'platform', 'linux'):
                self.assertEqual(tls_support.configure_tls(files[0])[0], str(files[0].resolve()))
                self.assertEqual(tls_support.configure_tls()[0], str(files[1].resolve()))
                del os.environ['NCFTA_CA_BUNDLE']
                self.assertEqual(tls_support.configure_tls()[0], str(files[2].resolve()))
                del os.environ['REQUESTS_CA_BUNDLE']
                self.assertEqual(tls_support.configure_tls()[0], str(files[3].resolve()))

    def test_verification_disable_and_missing_bundle_are_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            for value in (False, 'not-a-real-certificate-file.pem', True):
                with self.subTest(value=value), self.assertRaises(tls_support.TLSConfigurationError):
                    tls_support.configure_tls(value)

    def test_missing_windows_dependency_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(tls_support.sys, 'platform', 'win32'), \
             patch.object(tls_support, '_windows_initialized', False), patch.dict('sys.modules', {'truststore': None}):
            with self.assertRaisesRegex(tls_support.TLSConfigurationError, 'truststore==0.10.5'):
                tls_support.configure_tls()


if __name__ == '__main__':
    unittest.main()
