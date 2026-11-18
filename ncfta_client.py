"""Shared read-only PyMISP client with verified TLS and no redirects."""
from contextlib import contextmanager
import logging
from urllib.parse import urlsplit

from pymisp import PyMISP
from pymisp.exceptions import PyMISPError
import requests


class NCFTAClientError(Exception):
    """Sanitized connection error safe for terminal output."""


def safe_connection_error(exc):
    """Recover safe causes even when PyMISP wraps a request exception."""
    pending = [exc]
    seen = set()
    causes = []
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        causes.append(item)
        pending.extend(cause for cause in (item.__cause__, item.__context__)
                       if cause is not None)
    for item in causes:
        if isinstance(item, NCFTAClientError):
            return item
    for kind, message in (
        (requests.exceptions.SSLError, 'TLS certificate verification failed. Ask IT for the trusted CA bundle; do not disable verification.'),
        (requests.exceptions.ProxyError, 'Proxy connection failed. Check the work PC proxy or VPN settings with IT.'),
        (requests.exceptions.Timeout, 'Connection timed out. Check the work PC network, VPN and server availability.'),
        (requests.exceptions.ConnectionError, 'Network connection failed. Check DNS, firewall, VPN and server availability.'),
        (requests.exceptions.InvalidHeader, 'API key header is invalid. Check the key formatting in .env.'),
    ):
        if any(isinstance(item, kind) for item in causes):
            return NCFTAClientError(message)
    return NCFTAClientError('PyMISP startup or API request failed. Run Check-Connection.bat to check endpoint access; raw server details remain hidden.')


class ReadOnlyAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, origin):
        super().__init__()
        self.origin = origin

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        origin = (url.scheme, url.netloc)
        if origin != self.origin:
            raise NCFTAClientError('Request to another server refused.')
        if request.method != 'GET' and not (
                request.method == 'POST' and url.path.rstrip('/') == '/events/restSearch'):
            raise NCFTAClientError('Operation refused by the read-only client.')
        if not kwargs.get('verify', True):
            raise NCFTAClientError('TLS certificate verification must remain enabled.')
        response = super().send(request, **kwargs)
        if 300 <= response.status_code < 400:
            response.close()
            raise NCFTAClientError('Redirect refused. Check the server endpoint.')
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise NCFTAClientError(f'MISP request failed: HTTP {status}. Check permissions and server availability.')
        return response


@contextmanager
def connect(url, key, timeout, ca_bundle=None):
    # PyMISP may log response bodies and headers. Keep those out of CLI logs.
    logger = logging.getLogger('pymisp')
    previous_disabled = logger.disabled
    logger.disabled = True
    parsed = urlsplit(url)
    adapter = ReadOnlyAdapter((parsed.scheme, parsed.netloc))
    try:
        client = PyMISP(url, key, ssl=str(ca_bundle) if ca_bundle else True,
                        timeout=timeout, debug=False, https_adapter=adapter,
                        tool='NCFTA-ReadOnly-CLI')
        yield client
    except NCFTAClientError:
        raise
    except (PyMISPError, requests.exceptions.RequestException) as exc:
        raise safe_connection_error(exc) from None
    finally:
        adapter.close()
        logger.disabled = previous_disabled

