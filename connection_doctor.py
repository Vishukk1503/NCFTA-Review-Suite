"""Bounded, read-only NCFTA diagnosis. No suite/model imports or private output."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import multiprocessing as mp
import os
from pathlib import Path
import socket
import sys
import time
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
VERSION = '1.0'
MAX_RESPONSE = 8 * 1024 * 1024


class ConfigError(Exception):
    pass


def result(code, message, **values):
    return dict(code=code, message=message, **values)


def configuration(root):
    path = root / 'settings.json'
    if not path.exists():
        path = root / 'settings.example.json'
    try:
        cfg = json.loads(path.read_text(encoding='utf-8-sig'))
        url = cfg['url']
        parsed = urlsplit(url)
        port = parsed.port or 443
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ConfigError('Settings missing/invalid. URL must be an HTTPS origin without a path or credentials.') from None
    env_path = root / '.env'
    if not env_path.is_file():
        env_path = Path(os.path.expandvars(cfg.get('env', '.env'))).expanduser()
        if not env_path.is_absolute():
            env_path = root / env_path
    key = os.environ.get('NCFTA_API_KEY') or os.environ.get('MISP_API_KEY')
    key_source = 'process environment' if key else 'env file'
    if not key and env_path.is_file():
        try:
            for raw in env_path.read_text(encoding='utf-8-sig').splitlines():
                line = raw.strip().removeprefix('export ')
                if not line or line.startswith('#') or '=' not in line:
                    continue
                name, value = line.split('=', 1)
                if name.strip() in ('NCFTA_API_KEY', 'MISP_API_KEY', 'MISP_KEY'):
                    value = value.strip()
                    if len(value) > 1 and value[0] == value[-1] and value[0] in '\"\'':
                        value = value[1:-1]
                    else:
                        value = value.split(' #', 1)[0].strip()
                    if value:
                        key = value
                        break
        except (OSError, UnicodeError):
            raise ConfigError('Cannot read the configured env file.') from None
    key_issue = ''
    if not key:
        key_issue = 'API key missing. Save .env, not .env.txt, beside Start-NCFTA.bat.'
    elif (not key.isascii() or any(c.isspace() for c in key)
          or key in ('YOUR_ACTUAL_KEY', 'YOUR_NCFTA_API_KEY', 'PASTE_YOUR_KEY_HERE')):
        key_issue = 'API key contains whitespace, non-ASCII text or a placeholder.'
    bundle = cfg.get('ca_bundle')
    if bundle is False:
        raise ConfigError('ca_bundle=false is invalid; TLS verification stays enabled.')
    bundle = bundle or os.environ.get('NCFTA_CA_BUNDLE') or os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('CURL_CA_BUNDLE')
    if bundle:
        try:
            bundle = Path(os.path.expandvars(bundle)).expanduser()
            if not bundle.is_absolute():
                bundle = root / bundle
            if not bundle.is_file():
                raise ValueError()
            bundle = str(bundle.resolve())
        except (ValueError, TypeError):
            raise ConfigError('Configured CA bundle is not an existing file.') from None
    transport = cfg.get('transport', 'auto')
    if transport not in ('auto', 'requests', 'pymisp'):
        raise ConfigError('Invalid transport setting; use auto, requests or pymisp.')
    return dict(url=url, host=parsed.hostname, port=port, key=key or '',
                key_issue=key_issue, key_source=key_source, ca_bundle=bundle,
                transport=transport)


def proxy_summary(url):
    import requests
    proxies = requests.utils.get_environ_proxies(url)
    effective = bool(requests.utils.select_proxy(url, proxies))
    info = dict(requests_proxy=effective,
                proxy_env=any(os.environ.get(k) for k in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy')),
                bypass_env=bool(os.environ.get('NO_PROXY') or os.environ.get('no_proxy')))
    if sys.platform == 'win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as reg:
                for name, dest in (('ProxyEnable', 'windows_manual_proxy'), ('AutoConfigURL', 'windows_pac')):
                    try:
                        info[dest] = bool(winreg.QueryValueEx(reg, name)[0])
                    except OSError:
                        info[dest] = False
        except OSError:
            pass
    return info


def network_error(exc, requests):
    kinds = (
        (requests.exceptions.SSLError, 'TLS', 'Certificate/hostname validation failed. Use the IT-approved CA bundle; verification was not disabled.'),
        (requests.exceptions.ProxyError, 'PROXY', 'The configured proxy connection failed.'),
        (requests.exceptions.ConnectTimeout, 'CONNECT_TIMEOUT', 'Connection establishment timed out before an HTTP response.'),
        (requests.exceptions.ReadTimeout, 'READ_TIMEOUT', 'Timed out waiting for response data; the service or gateway did not respond in time.'),
        (requests.exceptions.Timeout, 'TIMEOUT', 'Request timed out.'),
        (requests.exceptions.ConnectionError, 'CONNECTION', 'Connection failed or was reset. DNS/TCP results below narrow this down.'),
        (requests.exceptions.InvalidHeader, 'KEY_FORMAT', 'Invalid API key header formatting.'),
    )
    for kind, code, message in kinds:
        if isinstance(exc, kind):
            return result(code, message)
    return result('REQUEST', 'Request failed; raw headers and body were not recorded.')


def http_error(status):
    messages = {
        401: 'Authentication rejected by the server or gateway; check the API key.',
        403: 'Access forbidden by the server or gateway; API/group permission or gateway policy may deny it.',
        404: 'Endpoint not found; check the configured origin and API routing.',
        407: 'Proxy authentication required.',
        429: 'Server/gateway rate limit; retry later.',
    }
    message = messages.get(status, 'Server/gateway returned an unsuccessful response.')
    if 300 <= status < 400:
        message = 'Redirect returned; not followed. API may be redirected to a login/gateway.'
    return result('HTTP_' + str(status), f'HTTP {status}: {message}', http_status=status)


def api_result(kind, payload, group=None, start=None, end=None):
    if isinstance(payload, dict) and ('error' in payload or 'errors' in payload):
        return result('API_ERROR', 'HTTP 200 contains an API error; body withheld.')
    if kind == 'version' and isinstance(payload, dict) and isinstance(payload.get('version'), str):
        return result('PASS', 'MISP version endpoint responded; this alone does not prove authentication.')
    if kind == 'auth' and isinstance(payload, dict) and isinstance(payload.get('User'), dict):
        return result('PASS', 'Authenticated user endpoint accepted the key; account details discarded.')
    if kind == 'types' and isinstance(payload, dict) and isinstance(payload.get('result'), dict) and isinstance(payload['result'].get('types'), list):
        return result('PASS', 'Attribute metadata endpoint responded.')
    if kind == 'events':
        rows = payload.get('response') if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return result('SHAPE', 'Event search did not return the expected event list.')
        if not rows:
            return result('EMPTY', 'Search succeeded with 0 visible events. Empty results do not prove access to this group.', events=0)
        if len(rows) > 1:
            return result('SCOPE', 'Server did not respect the one-event limit; event data discarded.')
        event = rows[0].get('Event', rows[0]) if isinstance(rows[0], dict) else None
        if not isinstance(event, dict):
            return result('SHAPE', 'Unexpected event response structure; body discarded.')
        if str(event.get('sharing_group_id')) != str(group) or not start <= str(event.get('date', '')) <= end:
            return result('SCOPE', 'Returned event does not match the requested group/date; filters need investigation.')
        return result('PASS', 'One matching event received and discarded. Fetch access confirmed for this group/date.', events=1)
    return result('SHAPE', 'HTTP 200 JSON is not the expected MISP response; possible API/gateway mismatch.')


def http_probe(cfg, kind):
    # Native trust is enabled before importing requests, matching the suite fix.
    if sys.platform == 'win32':
        import truststore
        truststore.inject_into_ssl()
    import requests
    paths = {'login': '/users/login', 'version': '/servers/getVersion',
             'auth': '/users/view/me', 'types': '/attributes/describeTypes.json',
             'events': '/events/restSearch'}
    headers = {'Accept': 'application/json'}
    if kind != 'login':
        headers['Authorization'] = cfg['key']
    args = dict(headers=headers, timeout=(5, 15), allow_redirects=False,
                verify=cfg.get('ca_bundle') or True, stream=True)
    method = 'GET'
    if kind == 'events':
        method = 'POST'
        args['json'] = dict(returnFormat='json', sharinggroup=[cfg['group']],
                            **{'from': cfg['start'], 'to': cfg['end']},
                            page=1, limit=1, metadata=False, withAttachments=False)
    try:
        with requests.Session() as session:
            with session.request(method, cfg['url'] + paths[kind], **args) as response:
                if kind == 'login':
                    if response.status_code == 200 or 300 <= response.status_code < 400:
                        return result('PASS', 'Verified HTTPS reached the login endpoint; no API key sent, no redirect followed.', http_status=response.status_code, tls_verified=True)
                    row = http_error(response.status_code)
                    row.update(tls_verified=True)
                    row['message'] += ' TLS verification passed; this is an HTTP refusal, not a certificate error.'
                    return row
                if response.status_code != 200:
                    return http_error(response.status_code)
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE:
                        return result('SIZE_LIMIT', 'Response exceeded the 8 MB diagnostic limit; fetch itself may require a longer/larger request.')
                try:
                    payload = json.loads(data)
                except (ValueError, UnicodeError):
                    return result('NOT_JSON', 'HTTP 200 is not JSON; possible sign-in page or gateway response. Body discarded.')
                return api_result(kind, payload, cfg.get('group'), cfg.get('start'), cfg.get('end'))
    except requests.exceptions.RequestException as exc:
        return network_error(exc, requests)


def worker(conn, kind, cfg):
    try:
        if kind == 'dns':
            entries = socket.getaddrinfo(cfg['host'], cfg['port'], type=socket.SOCK_STREAM)
            row = result('PASS', 'Hostname resolved.', address_count=len({entry[4][0] for entry in entries}))
        elif kind == 'tcp':
            with socket.create_connection((cfg['host'], cfg['port']), timeout=5):
                row = result('PASS', 'Direct TCP connection opened. A proxy can use a different route.')
        elif kind == 'proxy':
            info = proxy_summary(cfg['url'])
            row = result('INFO', 'Proxy settings inspected; addresses/credentials omitted.', **info)
        else:
            row = http_probe(cfg, kind)
    except socket.gaierror:
        row = result('DNS', 'Local DNS resolution failed. A proxy may resolve the host separately.')
    except (TimeoutError, socket.timeout):
        row = result('TIMEOUT', 'Direct network operation timed out; proxy-based HTTPS may still work.')
    except ImportError:
        row = result('DEPENDENCY', 'Required requests/truststore package missing in the selected Python environment.')
    except OSError:
        row = result('CONNECTION', 'Direct connection refused/unreachable, or local network operation failed.')
    except Exception:
        row = result('INTERNAL', 'Diagnostic step could not complete; no raw exception details recorded.')
    try:
        conn.send(row)
    finally:
        conn.close()


def run_step(kind, label, cfg, seconds=25, target=worker):
    print(f'\nCHECK {label} (maximum {seconds}s)', flush=True)
    start = time.monotonic()
    ctx = mp.get_context('spawn')
    receive, send = ctx.Pipe(duplex=False)
    process = ctx.Process(target=target, args=(send, kind, cfg), daemon=True)
    process.start()
    send.close()
    next_update = 5
    row = None
    try:
        while time.monotonic() - start < seconds:
            if receive.poll(.2):
                try:
                    row = receive.recv()
                except EOFError:
                    row = result('WORKER', 'Check process exited unexpectedly.')
                break
            elapsed = time.monotonic() - start
            if elapsed >= next_update:
                print(f'  Still checking: {int(elapsed)}s / {seconds}s', flush=True)
                next_update += 5
            if not process.is_alive():
                row = result('WORKER', 'Check process exited unexpectedly.')
                break
        if row is None:
            row = result('DEADLINE', f'No completed result within {seconds}s. Step stopped; no retries.')
    finally:
        if process.is_alive():
            process.terminate()
        process.join(2)
        if process.is_alive():
            process.kill()
            process.join(1)
        receive.close()
    row.update(step=kind, label=label, seconds=round(time.monotonic() - start, 2))
    if kind == 'events':
        row['group'] = cfg['group']
    print(f"{row['code']} [{row['seconds']}s] {row['message']}", flush=True)
    if kind == 'proxy' and row['code'] == 'INFO':
        for name in ('requests_proxy', 'proxy_env', 'bypass_env', 'windows_manual_proxy', 'windows_pac'):
            if name in row:
                print(f'  {name}: {row[name]}')
        if row.get('windows_pac') and not row.get('requests_proxy'):
            print('  Windows PAC is configured but requests selected no proxy. Requests does not evaluate PAC scripts.')
    return row


def date_value(value):
    for pattern in ('%Y-%m-%d', '%m/%d/%Y'):
        try:
            return dt.datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            pass
    raise ValueError('Use YYYY-MM-DD or MM/DD/YYYY.')


def filters(args):
    while True:
        try:
            start = date_value(args.from_date or input('Start event date [YYYY-MM-DD or MM/DD/YYYY]: '))
            end = date_value(args.to_date or input(f'End event date [{start}]: ').strip() or start)
            if start > end:
                raise ValueError('Start date must not be after end date.')
            raw = args.groups or input('Groups [all = 6 7 8]: ').strip() or 'all'
            groups = [6, 7, 8] if raw.lower() == 'all' else sorted({int(x) for x in raw.replace(',', ' ').split()})
            if not groups or not set(groups) <= {6, 7, 8}:
                raise ValueError('Groups must be 6, 7, 8 or all.')
            return start, end, groups
        except ValueError as exc:
            print('Invalid date/groups. Use valid dates and groups 6, 7, 8 or all.')
            if args.from_date or args.to_date or args.groups:
                raise ConfigError('Invalid date/group arguments.') from None


def diagnosis(rows):
    by_kind = {row['step']: row for row in rows}
    api = [r for r in rows if r['step'] in ('version', 'auth', 'types', 'events')]
    event_rows = [r for r in api if r['step'] == 'events']
    failures = [r for r in api if r['code'] not in ('PASS', 'EMPTY')]
    messages = []
    if failures:
        messages.append('Failed API checks: ' + '; '.join(r['label'] + ' = ' + r['code'] for r in failures) + '.')
    login = by_kind.get('login', {})
    if login.get('code') == 'PASS' and failures:
        messages.append('Python HTTPS reaches the login page. The API path still fails; browser login availability does not establish API access.')
    if any(r['code'] in ('TLS', 'PROXY', 'HTTP_407') for r in rows):
        messages.append('Fix the reported trust/proxy issue using the approved work-network settings. Do not disable certificate checks.')
    if any(r['code'] in ('HTTP_401', 'HTTP_403') for r in api):
        messages.append('The server or gateway denied a request. This check cannot distinguish gateway policy from NCFTA permissions without server-side evidence.')
    if any(r['code'] in ('DEADLINE', 'CONNECT_TIMEOUT', 'READ_TIMEOUT', 'TIMEOUT', 'CONNECTION') for r in api):
        messages.append('The request did not complete. A timeout does not prove the API key is wrong; no automatic retry was made.')
    if event_rows and not any(r['code'] not in ('PASS', 'EMPTY') for r in event_rows):
        messages.append('Event search completed for every selected group. EMPTY means no visible match, not verified group permission.')
    if not event_rows:
        messages.append('Event fetching was not tested.')
    if by_kind.get('auth', {}).get('code') == 'PASS':
        messages.append('The authenticated user endpoint accepted the configured key.')
        if login.get('http_status') in (401, 403):
            messages.append('Anonymous login-page request was denied, but authenticated API access worked. These are different checks.')
    if not messages:
        messages.append('Checks incomplete; read the failed stage above.')
    return messages


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata-only', action='store_true')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--groups', help='all, 6, 7, 8, or a quoted space/comma-separated list')
    parser.add_argument('--suite-dir', type=Path, default=HERE)
    args = parser.parse_args(argv)
    print(f'NCFTA Connection Doctor {VERSION} | BAT + Python | no model loading', flush=True)
    print('No settings changed. No keys, response bodies or event values printed/saved.')
    print('Each check has a hard deadline and no retries. Direct DNS/TCP failures do not rule out proxy access.')
    root = args.suite_dir.resolve()
    report = dict(version=VERSION, utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  python='.'.join(map(str, sys.version_info[:3])), model_loaded=False,
                  event_requests=0, checks=[], ok=False)
    started = time.monotonic()
    try:
        cfg = configuration(root)
        print('Settings parsed. TLS: Windows certificate store' if sys.platform == 'win32' else 'Settings parsed. TLS: default verified trust')
        print('Additional CA bundle: ' + ('configured' if cfg['ca_bundle'] else 'none'))
        print('Key source: ' + cfg['key_source'] + '; suite transport: ' + cfg['transport'])
        print('This utility tests direct requests; it does not require PyMISP startup.')
        if cfg['key_issue']:
            print('KEY_FORMAT: ' + cfg['key_issue'])
            report['key_issue'] = cfg['key_issue']
        for module in ('requests', 'truststore'):
            if (module == 'requests' or sys.platform == 'win32') and importlib.util.find_spec(module) is None:
                raise ConfigError('Missing ' + module + '. Run this from the installed suite Python environment.')
        selection = None
        if not args.metadata_only and not cfg['key_issue']:
            print('\nEnter the SAME dates/groups that failed. Only the first event per group will be requested, then discarded.')
            selection = filters(args)
            report['filters'] = dict(start=selection[0], end=selection[1], groups=selection[2])
        for kind, label, timeout in [('proxy', 'Proxy configuration', 10), ('dns', 'Local DNS', 10),
                                      ('tcp', 'Direct TCP port', 12), ('login', 'TLS + login HTTPS (no key)', 25)]:
            report['checks'].append(run_step(kind, label, cfg, timeout))
        if not cfg['key_issue']:
            for kind, label in [('version', 'GET /servers/getVersion'), ('auth', 'GET /users/view/me'),
                                ('types', 'GET /attributes/describeTypes.json')]:
                report['checks'].append(run_step(kind, label, cfg))
            if selection:
                start, end, groups = selection
                for group in groups:
                    report['event_requests'] += 1
                    report['checks'].append(run_step('events', f'POST /events/restSearch group {group}',
                                                     dict(cfg, group=group, start=start, end=end), 30))
        api = [r for r in report['checks'] if r['step'] in ('version', 'auth', 'types', 'events')]
        report['ok'] = bool(api) and not cfg['key_issue'] and all(r['code'] in ('PASS', 'EMPTY') for r in api)
        del cfg
    except ConfigError as exc:
        report['error'] = str(exc)
        print('CONFIG: ' + str(exc))
    except (KeyboardInterrupt, EOFError):
        report['error'] = 'Stopped by user/input closed.'
        print(report['error'])
    except Exception:
        report['error'] = 'Utility could not complete; no private exception details recorded.'
        print(report['error'])
    report['summary'] = diagnosis(report['checks'])
    print('\nRESULT: ' + ('API CHECKS PASSED' if report['ok'] else 'CHECKS FAILED OR INCOMPLETE'))
    for message in report['summary']:
        print('  ' + message)
    report['elapsed_seconds'] = round(time.monotonic() - started, 2)
    try:
        directory = root / 'setup'
        directory.mkdir(exist_ok=True)
        (directory / 'connection-doctor.json').write_text(json.dumps(report, indent=2), encoding='utf8')
        print('Saved: setup/connection-doctor.json. All results are also on this screen.')
    except OSError:
        print('Report could not be saved; results remain above.')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    mp.freeze_support()
    raise SystemExit(main())
