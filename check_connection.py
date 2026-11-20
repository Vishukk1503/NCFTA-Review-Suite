"""Metadata-only connection diagnosis for existing Windows suite installations.

Never fetches events, loads the model, prints response bodies or prints keys.
Works with older suites without replacing their files or installing packages.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from tls_support import configure_tls, TLSConfigurationError

HERE = Path(__file__).resolve().parent
ENDPOINTS = ('/servers/getVersion', '/users/view/me', '/attributes/describeTypes.json')


def quick_worker(conn, kind, cfg):
    """One authenticated metadata GET, isolated so even DNS has a deadline."""
    from connection_doctor import http_probe
    try:
        verify, trust = configure_tls(cfg.get('ca_bundle'))
        row = http_probe(dict(cfg, ca_bundle=verify if verify is not True else None), 'auth')
        row['tls_trust'] = trust
    except TLSConfigurationError as exc:
        row = dict(code='TLS_CONFIG', message=str(exc))
    except ImportError:
        row = dict(code='DEPENDENCY', message='Connection-check dependencies are missing. Run Install-Work-PC.bat.')
    except Exception:
        row = dict(code='CHECK_ERROR', message='Connection check could not complete. Run Connection-Doctor.bat --metadata-only for diagnosis.')
    # Only the compact status leaves the worker. No account/body/header data.
    allowed = ('code', 'message', 'http_status', 'tls_verified', 'tls_trust', 'content_type')
    try:
        conn.send({name: row[name] for name in allowed if name in row})
    finally:
        conn.close()


def quick_check(cfg):
    """Use the suite's key/settings; never fetch events or start the model."""
    import suite
    from connection_doctor import run_step
    start = time.monotonic()
    try:
        # This small probe uses Requests even when event fetching uses PyMISP.
        suite.transport(dict(cfg, transport='requests'))  # Validate the HTTPS origin.
        key = suite.credentials(cfg)
        if (not key.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key)
                or key in ('YOUR_ACTUAL_KEY', 'YOUR_NCFTA_API_KEY', 'PASTE_YOUR_KEY_HERE')):
            raise suite.SuiteError('API key contains a placeholder or invalid characters. Check .env; do not share the key.')
        row = run_step('auth', 'NCFTA API: GET /users/view/me',
                       dict(url=cfg['url'], key=key, ca_bundle=cfg.get('ca_bundle')),
                       seconds=20, target=quick_worker, quiet=True)
        del key
    except suite.SuiteError as exc:
        row = dict(code='CONFIG', message=str(exc))
    except Exception:
        row = dict(code='CHECK_ERROR', message='Connection check could not complete. Run Connection-Doctor.bat --metadata-only for diagnosis.')
    row.setdefault('seconds', round(time.monotonic() - start, 2))
    row['ok'] = row['code'] == 'PASS'
    return row


def network_message(exc, requests):
    for kind, message in (
        (requests.exceptions.SSLError, 'TLS certificate verification failed. Ask IT for the trusted CA bundle; do not disable verification.'),
        (requests.exceptions.ProxyError, 'Proxy connection failed. Check proxy/VPN settings with IT.'),
        (requests.exceptions.Timeout, 'Request timed out. Check network, VPN and server availability.'),
        (requests.exceptions.ConnectionError, 'Network connection failed. Check DNS, firewall, VPN and server availability.'),
        (requests.exceptions.InvalidHeader, 'API key header is invalid. Check key formatting in .env.'),
    ):
        if isinstance(exc, kind):
            return message
    return 'Request failed. Raw exception text is withheld to protect credentials.'


def http_message(status):
    if status in (401, 403):
        return f'HTTP {status}: access denied by the server or gateway. Check the key, API permissions and work-network access.'
    if 300 <= status < 400:
        return f'HTTP {status}: redirect refused. Check the configured MISP address or work-network sign-in requirements.'
    if status == 404:
        return 'HTTP 404: API endpoint not found. Check the MISP address or proxy routing.'
    if status == 429:
        return 'HTTP 429: server rate limit reached. Retry later.'
    return f'HTTP {status}: unexpected server or gateway response.'


def expected_json(path, payload):
    if not isinstance(payload, dict) or 'error' in payload or 'errors' in payload:
        return False
    if path == ENDPOINTS[0]:
        return isinstance(payload.get('version'), str)
    if path == ENDPOINTS[1]:
        return isinstance(payload.get('User'), dict)
    return isinstance(payload.get('result'), dict) and isinstance(payload['result'].get('types'), list)


def probe(url, key, session, requests, verify=True):
    # Authentication headers go only to this validated HTTPS origin. Never
    # follow a redirect or include any returned headers/body in the report.
    results = []
    for path in ENDPOINTS:
        start = time.perf_counter()
        row = {'endpoint': path, 'ok': False}
        try:
            with session.get(url + path,
                             headers={'Authorization': key, 'Accept': 'application/json'},
                             timeout=(5, 10), allow_redirects=False, verify=verify) as response:
                row['http_status'] = response.status_code
                if response.status_code != 200:
                    row['message'] = http_message(response.status_code)
                else:
                    try:
                        payload = response.json()
                        row['ok'] = expected_json(path, payload)
                        del payload
                        row['message'] = 'Metadata endpoint passed.' if row['ok'] else 'HTTP 200, but the JSON is not the expected MISP response. Check API compatibility or gateway filtering.'
                    except ValueError:
                        row['message'] = 'HTTP 200, but response is not JSON. A sign-in page or gateway may be intercepting the request.'
        except requests.exceptions.RequestException as exc:
            row['message'] = network_message(exc, requests)
        row['seconds'] = round(time.perf_counter() - start, 3)
        results.append(row)
        print(f"{'PASS' if row['ok'] else 'FAIL'}  {path}: {row['message']}", flush=True)
        if not row['ok']:
            break
    return results


def main():
    print('NCFTA connection check - metadata only; no events or model inference.', flush=True)
    started = time.perf_counter()
    report = {'event_requests': 0, 'model_loaded': False, 'ok': False, 'checks': []}
    try:
        import suite
        cfg = suite.settings()
        verify, report['tls_trust'] = configure_tls(cfg.get('ca_bundle'))
        print('TLS trust: ' + report['tls_trust'] + '; verification enabled.', flush=True)
        import requests
        suite.transport(cfg)  # Validates the HTTPS origin, without a request.
        key = suite.credentials(cfg)
        if not key.isascii() or any(c.isspace() for c in key) or key in ('YOUR_ACTUAL_KEY', 'YOUR_NCFTA_API_KEY', 'PASTE_YOUR_KEY_HERE'):
            raise suite.SuiteError('The API key appears to contain a placeholder, whitespace or non-ASCII text. Check .env; do not share the key.')
        with requests.Session() as session:
            report['checks'] = probe(cfg['url'], key, session, requests, verify=verify)
        del key
        report['ok'] = len(report['checks']) == len(ENDPOINTS) and all(row['ok'] for row in report['checks'])
        if report['ok']:
            print('PASS: all three metadata endpoints are accessible from this PC.')
            print('This does not test event/group access. If fetching still fails, share this report and the error line.')
    except Exception as exc:
        # Only SuiteError is defined to contain safe local validation messages.
        module = sys.modules.get('suite')
        safe_error = isinstance(exc, TLSConfigurationError) or (module and isinstance(exc, module.SuiteError))
        report['message'] = str(exc) if safe_error else 'Connection check could not complete (' + type(exc).__name__ + '). No raw exception details were printed.'
        print('FAIL: ' + report['message'])
    report['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    target = HERE / 'setup' / 'connection-report.json'
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding='utf8')
        print('Report: ' + str(target))
    except OSError:
        print('Could not save the report; the results above are still valid.')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
