"""Application TLS setup: use Windows trust without bypassing verification."""
from __future__ import annotations

import os
from pathlib import Path
import sys

_windows_initialized = False


class TLSConfigurationError(Exception):
    """Safe local configuration message; never contains credentials."""


def configure_tls(ca_bundle=None):
    """Configure before creating HTTP sessions; return verify value and a label.

    Native Windows validation uses certificates managed by Windows/IT. An
    optional PEM adds trusted certificates; no certificate is installed into
    Windows and certificate/hostname validation remains required.
    """
    global _windows_initialized
    if ca_bundle is False:
        raise TLSConfigurationError('TLS verification cannot be disabled.')
    bundle = (ca_bundle or os.environ.get('NCFTA_CA_BUNDLE')
              or os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('CURL_CA_BUNDLE'))
    verify = True
    if bundle:
        if not isinstance(bundle, (str, os.PathLike)):
            raise TLSConfigurationError('ca_bundle must be the path to a trusted PEM file.')
        path = Path(os.path.expandvars(str(bundle))).expanduser()
        if not path.is_file():
            raise TLSConfigurationError('Configured CA bundle does not exist. Check ca_bundle or the CA bundle environment variable.')
        verify = str(path.resolve())
    mode = 'Default certificate bundle'
    if sys.platform == 'win32':
        if not _windows_initialized:
            try:
                import truststore
            except ImportError:
                raise TLSConfigurationError('Windows certificate support is missing. Install truststore==0.10.5 in this suite environment.') from None
            # This is application setup, intentionally before HTTP sessions.
            # Truststore also updates requests/urllib3 if already imported.
            truststore.inject_into_ssl()
            _windows_initialized = True
        mode = 'Windows certificate store'
    if verify is not True:
        mode += ' + configured CA bundle'
    return verify, mode
