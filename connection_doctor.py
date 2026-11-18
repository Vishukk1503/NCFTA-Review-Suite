"""Bounded, read-only NCFTA diagnosis. No suite/model imports or private output."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
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
VERSION = '1.4'
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
    file_keys = []
    if env_path.is_file():
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
                        file_keys.append(value)
        except (OSError, UnicodeError):
            if not key:
                raise ConfigError('Cannot read the configured env file.') from None
    key_conflicts = dict(env_overrides_different_file_key=bool(key and file_keys and any(v != key for v in file_keys)),
                         multiple_different_file_keys=len(set(file_keys)) > 1,
                         env_variables_disagree=bool(os.environ.get('NCFTA_API_KEY') and os.environ.get('MISP_API_KEY')
                                                    and os.environ['NCFTA_API_KEY'] != os.environ['MISP_API_KEY']),
                         env_txt_present=(root / '.env.txt').is_file())
    if not key and file_keys:
        key = file_keys[0]
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
                transport=transport, key_conflicts=key_conflicts)


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
        from connection_windows import proxy_config
        info.update(proxy_config())
    return info


def network_error(exc, requests):
    kinds = (
        (requests.exceptions.ContentDecodingError, 'DECOMPRESSION', 'Response decompression failed; compare the identity-encoding probe.'),
        (requests.exceptions.ChunkedEncodingError, 'BODY_INTERRUPTED', 'Response body ended unexpectedly or had invalid transfer encoding.'),
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
        flags = {}
        user = payload['User']
        role = payload.get('Role', user.get('Role', {}))
        if isinstance(role, dict) and role.get('perm_auth') in (True, False, 0, 1, '0', '1'):
            flags['role_api_permission'] = role['perm_auth'] in (True, 1, '1')
        return result('PASS', 'User endpoint returned expected MISP user JSON; account details discarded.', **flags)
    if kind == 'sharing_groups':
        rows = payload.get('response', payload.get('SharingGroup')) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return result('SHAPE', 'Sharing-group metadata has an unrecognized shape; visibility remains unconfirmed.')
        ids = {str(item.get('SharingGroup', item).get('id')) for item in rows
               if isinstance(item, dict) and isinstance(item.get('SharingGroup', item), dict)}
        return result('INFO', 'Sharing-group listing inspected; names/members discarded. Absence does not prove denial.',
                      selected_groups_listed={str(group): str(group) in ids for group in (6, 7, 8)})
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
        objects = event.get('Object', [])
        direct = event.get('Attribute', [])
        if (not isinstance(objects, list) or not isinstance(direct, list)
                or any(not isinstance(obj, dict) or not isinstance(obj.get('Attribute', []), list) for obj in objects)):
            return result('SHAPE', 'Event objects/attributes have an unexpected structure; body discarded.')
        nested_count = sum(len(obj.get('Attribute', [])) for obj in objects)
        return result('PASS', 'One matching event fetched. Fetch access confirmed for this group/date.',
                      events=1, objects=len(objects), direct_attributes=len(direct),
                      object_attributes=nested_count, attributes=len(direct) + nested_count)
    return result('SHAPE', 'HTTP 200 JSON is not the expected MISP response; possible API/gateway mismatch.')


def response_facts(response, data, key=None):
    """Only fixed categories/booleans leave this function, never page text."""
    content_type = str(response.headers.get('Content-Type', '')).split(';', 1)[0].strip().lower()
    allowed_types = ('application/json', 'text/json', 'text/html', 'application/xhtml+xml',
                     'text/plain', 'application/xml', 'text/xml', 'application/octet-stream')
    facts = dict(http_status=response.status_code, tls_verified=True,
                 content_type=content_type if content_type in allowed_types else 'other-or-missing',
                 response_bytes=len(data))
    # Values from these headers are classified, never copied into the report.
    encoding = str(response.headers.get('Content-Encoding', '')).lower().strip()
    facts['content_encoding'] = encoding if encoding in ('', 'gzip', 'deflate', 'br', 'zstd', 'identity') else 'other'
    facts['via_header_present'] = bool(response.headers.get('Via'))
    server = str(response.headers.get('Server', '')).lower()
    facts['server_product_markers'] = [name for name in ('cloudflare', 'nginx', 'apache', 'microsoft-iis', 'awselb', 'zscaler', 'netskope') if name in server]
    challenge = str(response.headers.get('WWW-Authenticate', '')).lower()
    facts['http_auth_schemes'] = [name for name in ('basic', 'bearer', 'negotiate', 'ntlm') if name in challenge]
    from connection_rca import correlation_facts
    facts.update(correlation_facts(response.headers))
    if key and facts.get('correlation_ids'):
        facts['correlation_ids'] = {name: value for name, value in facts['correlation_ids'].items() if value != key}
    location = str(response.headers.get('Location', '')).lower()
    facts['redirect_markers'] = [name for name, fragment in [('misp_login', '/users/login'), ('microsoft_sign_in', 'login.microsoftonline.com'), ('saml', 'saml')]
                                if fragment in location]
    prepared = getattr(response, 'request', None)
    if key is not None and isinstance(getattr(prepared, 'headers', None), dict):
        facts['authorization_header_preserved'] = prepared.headers.get('Authorization') == key
    elif key is not None and prepared is not None:
        # requests uses CaseInsensitiveDict, rather than a plain dict.
        from collections.abc import Mapping
        if isinstance(getattr(prepared, 'headers', None), Mapping):
            facts['authorization_header_preserved'] = prepared.headers.get('Authorization') == key
    sample = bytes(data[:512 * 1024]).decode('utf-8', errors='ignore').lower()
    html = content_type in ('text/html', 'application/xhtml+xml') or any(
        marker in sample for marker in ('<!doctype html', '<html', '<form', '<title'))
    facts['body_kind'] = 'empty' if not data else ('html' if html else 'other')
    markers = []
    if html:
        if 'data[user][password]' in sample and ('data[user][email]' in sample or 'misp' in sample):
            markers.append('misp_login_form')
        if any(x in sample for x in ('login.microsoftonline.com', 'login.windows.net', 'sign in to your account', 'samlrequest', 'okta-sign-in')):
            markers.append('identity_sign_in')
        if any(x in sample for x in ('cf-chl-', '/cdn-cgi/challenge-platform/', 'verify you are human', 'g-recaptcha', 'hcaptcha')):
            markers.append('browser_challenge')
        if any(x in sample for x in ('access denied', 'request blocked', 'web page blocked', 'url blocked', 'website blocked', 'policy violation')):
            markers.append('access_block_message')
        if 'zscaler' in sample:
            markers.append('zscaler_reference')
        if 'netskope' in sample:
            markers.append('netskope_reference')
        if 'forcepoint' in sample or 'websense' in sample:
            markers.append('forcepoint_reference')
        if 'blue coat' in sample or 'proxyexception' in sample:
            markers.append('proxy_block_reference')
        if 'squid' in sample and ('err_access_denied' in sample or 'access denied' in sample):
            markers.append('squid_block_reference')
        if 'incapsula' in sample or '_incapsula_resource' in sample:
            markers.append('imperva_reference')
        if 'akamai' in sample and ('access denied' in sample or 'reference #' in sample):
            markers.append('akamai_block_reference')
    facts['page_markers'] = markers
    # Used only to compare against the anonymous page; removed before reporting.
    facts['_body_digest'] = hashlib.sha256(data).hexdigest()
    return facts


def event_body(cfg):
    if cfg['group'] not in (6, 7, 8) or date_value(cfg['start']) > date_value(cfg['end']):
        raise ConfigError('Invalid event scope.')
    return dict(returnFormat='json', sharinggroup=[cfg['group']],
                **{'from': cfg['start'], 'to': cfg['end']}, page=1, limit=1,
                metadata=cfg.get('event_variant') == 'metadata', withAttachments=False)


def http_probe(cfg, kind):
    # Native trust is enabled before importing requests, matching the suite fix.
    if sys.platform == 'win32':
        import truststore
        truststore.inject_into_ssl()
    import requests
    paths = {'login': '/users/login', 'version': '/servers/getVersion',
             'auth': '/users/view/me', 'types': '/attributes/describeTypes.json',
             'events': '/events/restSearch', 'auth_anonymous': '/users/view/me',
             'auth_json': '/users/view/me.json', 'sharing_groups': '/sharing_groups/index.json'}
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    authenticated = kind not in ('login', 'auth_anonymous')
    if authenticated:
        headers['Authorization'] = cfg['key']
    args = dict(headers=headers, timeout=(5, 60 if cfg.get('event_variant') == 'long_timeout' else 15), allow_redirects=False,
                verify=cfg.get('ca_bundle') or True, stream=True)
    method = 'GET'
    if kind in ('login', 'auth_anonymous'):
        class NoImplicitAuth(requests.auth.AuthBase):
            def __call__(self, request):
                request.headers.pop('Authorization', None)
                return request
        args['auth'] = NoImplicitAuth()
    if kind == 'events':
        method = 'POST'
        args['json'] = event_body(cfg)
        if cfg.get('event_variant') == 'json_endpoint':
            paths['events'] += '.json'
        if cfg.get('event_variant') == 'identity':
            headers['Accept-Encoding'] = 'identity'
    try:
        with requests.Session() as session:
            with session.request(method, cfg['url'] + paths[kind], **args) as response:
                if 300 <= response.status_code < 400:
                    row = http_error(response.status_code)
                    row.update(response_facts(response, b'', cfg['key'] if authenticated else None))
                    row.update(body_kind='not_read', response_bytes=None)
                    row.pop('_body_digest', None)
                    return row
                data = bytearray()
                truncated = False
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if response.status_code != 200 and len(data) >= 65536:
                        data = data[:65536]
                        truncated = True
                        break
                    if len(data) > MAX_RESPONSE:
                        return result('SIZE_LIMIT', 'Response exceeded the 8 MB diagnostic limit; fetch itself may require a longer/larger request.')
                facts = response_facts(response, data, cfg['key'] if authenticated else None)
                if response.status_code != 200:
                    row = http_error(response.status_code)
                    facts['body_sample_truncated'] = truncated
                    if not truncated:
                        try:
                            json.loads(data)
                            facts['body_kind'] = 'json'
                        except (ValueError, UnicodeError):
                            pass
                    facts.pop('_body_digest', None)
                    row.update(facts)
                    return row
                if kind == 'login':
                    return result('PASS', 'Verified HTTPS returned a page; this does not establish that it is the real MISP login page.', **facts)
                if facts.get('authorization_header_preserved') is False:
                    return result('KEY_REPLACED', 'Prepared request did not retain the configured API-key header. Check implicit .netrc authentication.', **facts)
                try:
                    payload = json.loads(data)
                except (ValueError, UnicodeError):
                    return result('NOT_JSON', 'HTTP 200 is not valid JSON. See response type and fixed page markers below; body discarded.', **facts)
                row = api_result('auth' if kind in ('auth_json', 'auth_anonymous') else kind, payload, cfg.get('group'), cfg.get('start'), cfg.get('end'))
                facts['body_kind'] = 'json'
                facts.pop('_body_digest', None)
                row.update(facts)
                return row
    except requests.exceptions.RequestException as exc:
        return network_error(exc, requests)


def tls_certificate(cfg):
    import ssl
    if sys.platform == 'win32':
        import truststore
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    else:
        context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    if cfg.get('ca_bundle'):
        context.load_verify_locations(cafile=cfg['ca_bundle'])
    stores = {}
    if sys.platform == 'win32':
        for store in ('ROOT', 'CA'):
            try:
                stores[store] = len(ssl.enum_certificates(store))
            except OSError:
                stores[store] = None
    try:
        with socket.create_connection((cfg['host'], cfg['port']), timeout=5) as sock:
            with context.wrap_socket(sock, server_hostname=cfg['host']) as secure:
                cert = secure.getpeercert()
                issuer = [value for rdn in cert.get('issuer', ()) for name, value in rdn
                          if name in ('commonName', 'organizationName')]
                from connection_windows import safe_cert_name, product_markers
                return result('PASS', 'Direct TLS certificate chain and hostname validated. This is the direct route, not a configured HTTP proxy.',
                              tls_version=secure.version(), certificate_issuer=safe_cert_name(' / '.join(issuer)),
                              certificate_sha256=hashlib.sha256(secure.getpeercert(binary_form=True)).hexdigest(),
                              certificate_not_before=cert.get('notBefore'), certificate_not_after=cert.get('notAfter'),
                              issuer_product_markers=product_markers(' '.join(issuer)), certificate_store_counts=stores)
    except ssl.SSLCertVerificationError as exc:
        return result('TLS', 'Direct-route certificate verification failed.', certificate_verify_code=exc.verify_code, certificate_store_counts=stores)
    except ssl.SSLError:
        return result('TLS', 'Direct-route TLS negotiation failed.', certificate_store_counts=stores)


def worker(conn, kind, cfg):
    try:
        if kind == 'inventory':
            from connection_rca import inventory
            row = inventory(cfg)
        elif kind == 'events' and cfg.get('event_variant') == 'suite':
            from connection_rca import suite_event_probe
            row = suite_event_probe(cfg)
        elif kind == 'events' and cfg.get('event_variant') == 'native':
            if sys.platform == 'win32':
                from connection_windows import winhttp_request
                row = winhttp_request(cfg, '/events/restSearch', event_body(cfg))
            else:
                row = result('SKIP', 'WinHTTP is not available on this OS.')
        elif kind == 'dns':
            entries = socket.getaddrinfo(cfg['host'], cfg['port'], type=socket.SOCK_STREAM)
            row = result('PASS', 'Hostname resolved.', address_count=len({entry[4][0] for entry in entries}))
        elif kind == 'tcp':
            with socket.create_connection((cfg['host'], cfg['port']), timeout=5):
                row = result('PASS', 'Direct TCP connection opened. A proxy can use a different route.')
        elif kind == 'proxy':
            info = proxy_summary(cfg['url'])
            row = result('INFO', 'Proxy settings inspected; addresses/credentials omitted.', **info)
        elif kind == 'routes':
            if sys.platform == 'win32':
                from connection_windows import route_inventory
                row = result('INFO', 'Windows route and VPN/software indicators inspected without changing them.', **route_inventory(cfg['host'], cfg['port']))
            else:
                row = result('SKIP', 'Windows route inventory is not available on this OS.')
        elif kind == 'certificate':
            row = tls_certificate(cfg)
        elif kind == 'native_auth':
            if sys.platform == 'win32':
                from connection_windows import winhttp_get
                row = winhttp_get(cfg)
            else:
                row = result('SKIP', 'WinHTTP is not available on this OS.')
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
        row['variant'] = cfg.get('event_variant', 'python')
    print(f"{row['code']} [{row['seconds']}s] {row['message']}", flush=True)
    if row.get('content_type'):
        size = 'body not read' if row['response_bytes'] is None else str(row['response_bytes']) + ' bytes'
        print(f"  Response: {row['content_type']} | {row['body_kind']} | {size}")
        print('  Page markers: ' + (', '.join(row['page_markers']) or 'none identified'))
        print('  Encoding: ' + (row.get('content_encoding') or 'none') + '; server markers: ' + (', '.join(row.get('server_product_markers', [])) or 'none'))
        if row.get('redirect_markers'):
            print('  Redirect markers: ' + ', '.join(row['redirect_markers']))
        if row.get('http_auth_schemes'):
            print('  HTTP authentication challenge: ' + ', '.join(row['http_auth_schemes']))
    if row.get('authorization_header_preserved') is True:
        print('  Configured API-key header retained by Python: yes')
    if row.get('authorization_header_preserved') is False:
        print('  KEY HEADER REPLACED: the prepared request did not retain the configured API key. Check implicit .netrc authentication.')
    if kind == 'proxy' and row['code'] == 'INFO':
        for name in ('requests_proxy', 'proxy_env', 'bypass_env', 'windows_manual_proxy', 'windows_pac',
                     'windows_wpad', 'windows_connection_pac', 'windows_connection_proxy', 'winhttp_machine_proxy'):
            if name in row:
                print(f'  {name}: {row[name]}')
        if (row.get('windows_pac') or row.get('windows_connection_pac') or row.get('windows_wpad')) and not row.get('requests_proxy'):
            print('  Windows automatic proxy configuration is enabled; Requests selected no explicit proxy. The WinHTTP check compares Windows routing.')
        for browser, values in row.get('browser_proxy_policies', {}).items():
            print('  ' + browser + ' policy: ' + json.dumps(values))
    if kind == 'routes' and row['code'] == 'INFO':
        for route in row.get('routes', []):
            print('  Route: ' + route['family'] + ' / ' + route.get('interface_type', 'unknown') + ' / ' + (', '.join(route.get('product_markers', [])) or 'no known VPN marker'))
        print('  Known network software running: ' + (', '.join(row.get('known_network_software_running', [])) or 'none detected'))
        print('  Presence alone does not prove interception; filter-based VPNs may not have a visible tunnel.')
    if row.get('certificate_issuer'):
        print('  Certificate issuer: ' + row['certificate_issuer'])
    if kind == 'certificate' and row.get('certificate_not_after'):
        print('  Certificate valid until: ' + row['certificate_not_after'])
    if row.get('correlation_ids'):
        print('  Request correlation: ' + json.dumps(row['correlation_ids']))
    if kind == 'inventory':
        for key in ('python_version', 'packages', 'suite_runtime_matches', 'network_files', 'output_storage', 'recent_run_states'):
            if key in row:
                print('  ' + key + ': ' + json.dumps(row[key]))
    if row.get('selected_groups_listed'):
        print('  Selected groups present in listing: ' + json.dumps(row['selected_groups_listed']))
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
            raw = args.groups or 'all'
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
    markers = {marker for r in rows if r['step'] not in ('login', 'auth_anonymous') for marker in r.get('page_markers', [])}
    primary = by_kind.get('auth', {})
    native = by_kind.get('native_auth', {})
    if primary.get('code') != 'PASS' and native.get('code') == 'PASS':
        messages.append('Windows WinHTTP authenticates successfully while Python Requests fails. Investigate differences in proxy selection, HTTP handling and application filtering; this is a usable lead, not proof of a particular VPN cause.')
    if primary.get('code') == 'NOT_JSON' and native.get('code') == 'NOT_JSON':
        messages.append('Both independent HTTP clients receive non-JSON from the API. A Requests-only JSON parser failure is unlikely; inspect the page markers and network/server policy.')
    if by_kind.get('auth_json', {}).get('code') == 'PASS' and primary.get('code') != 'PASS':
        messages.append('The explicit .json endpoint works while the original endpoint fails. API routing/content negotiation differs; use this result to correct the client endpoint.')
    anonymous = by_kind.get('auth_anonymous', {})
    if anonymous.get('code') == 'PASS':
        messages.append('Unexpected: the user endpoint returned user-shaped JSON without an API key. Do not treat this as proof the configured key works; investigate the gateway/server response.')
    if primary.get('same_body_as_anonymous_api'):
        messages.append('Authenticated and anonymous requests to the SAME API endpoint returned identical non-JSON bodies. Adding the API key did not change that response.')
    cert = by_kind.get('certificate', {})
    if cert.get('certificate_sha256') and native.get('certificate_sha256') and cert['certificate_sha256'] != native['certificate_sha256']:
        messages.append('Direct TLS and Windows HTTP presented different leaf certificates. Proxy inspection or different load-balancer certificates could explain this; compare the issuers, not just the fingerprints.')
    if cert.get('issuer_product_markers'):
        messages.append('The direct-route certificate issuer contains a known network-security product marker. This suggests TLS inspection; it does not by itself show that inspection blocked the API.')
    if any(r.get('authorization_header_preserved') is False for r in api):
        messages.append('The HTTP client replaced the API-key header before sending. Check implicit .netrc authentication; this is a client-side authentication issue.')
    if 'misp_login_form' in markers:
        messages.append('An API response contains a MISP login-form signature. Check API authentication and header handling; this is not a certificate failure.')
    if 'identity_sign_in' in markers:
        messages.append('An API response contains an identity sign-in marker. The API route needs an approved non-browser authentication path.')
    if 'browser_challenge' in markers:
        messages.append('An API response contains a browser-challenge marker. Ask the service/network administrator for approved API access; the utility does not bypass the challenge.')
    if 'access_block_message' in markers:
        messages.append('An API response contains an access-block message. Ask IT/NCFTA to check the policy for this API route.')
    if any(r.get('same_body_as_login') is True for r in api):
        messages.append('At least one API response is byte-for-byte identical to the anonymous login response. A common login/gateway response is being returned instead of API JSON; this alone does not identify its origin.')
    if failures:
        messages.append('Failed API checks: ' + '; '.join(r['label'] + ' = ' + r['code'] for r in failures) + '.')
    login = by_kind.get('login', {})
    if login.get('code') == 'PASS' and failures:
        messages.append('Python HTTPS received a response at the login URL. The API path still fails; a successful login-URL request does not establish API access.')
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
    if by_kind.get('auth', {}).get('code') == 'PASS' and anonymous.get('code') != 'PASS':
        messages.append('The authenticated user endpoint accepted the configured key.')
        if login.get('http_status') in (401, 403):
            messages.append('Anonymous login-page request was denied, but authenticated API access worked. These are different checks.')
    if not messages:
        messages.append('Checks incomplete; read the failed stage above.')
    if failures and not any(x in markers for x in ('misp_login_form', 'identity_sign_in', 'browser_challenge', 'access_block_message')) and native.get('code') != 'PASS':
        messages.append('Root cause remains unconfirmed. This report does not justify blaming the VPN, key or certificates without a matching result above.')
    return messages


def event_summary(rows):
    messages = []
    for row in rows:
        if row['step'] != 'events':
            continue
        variant = row.get('variant', 'python')
        prefix = f"Group {row['group']}" + (f" [{variant}]" if variant != 'python' else '') + ': '
        if row['code'] == 'PASS':
            messages.append(prefix + f"fetched 1 event | {row['objects']} objects | {row['attributes']} attributes "
                            f"({row['direct_attributes']} direct, {row['object_attributes']} in objects).")
        elif row['code'] == 'EMPTY':
            messages.append(prefix + '0 visible events in this date range; group access remains unconfirmed.')
        else:
            messages.append(prefix + row['code'] + ' - ' + row['message'])
    return messages


def discard_body_digests(rows):
    login_digest = next((r.get('_body_digest') for r in rows if r['step'] == 'login'), None)
    anonymous = next((r.get('_body_digest') for r in rows if r['step'] == 'auth_anonymous'), None)
    for row in rows:
        digest = row.pop('_body_digest', None)
        if row['step'] != 'login' and digest and login_digest and row.get('response_bytes', 0):
            row['same_body_as_login'] = digest == login_digest
        if row['step'] in ('auth', 'native_auth', 'auth_json') and digest and anonymous and row.get('response_bytes', 0):
            row['same_body_as_anonymous_api'] = digest == anonymous


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata-only', action='store_true', help='Skip event fetching and date prompts')
    parser.add_argument('--events', action='store_true', help='Fetch up to one event per group (now the default)')
    parser.add_argument('--from-date')
    parser.add_argument('--to-date')
    parser.add_argument('--groups', help='all, 6, 7, 8, or a quoted space/comma-separated list')
    parser.add_argument('--suite-dir', type=Path, default=HERE)
    args = parser.parse_args(argv)
    print(f'NCFTA Connection Doctor {VERSION} | BAT + Python | no model loading', flush=True)
    print('No settings changed. No keys, response bodies or event values printed/saved.')
    print('Each check has a hard deadline; no retry loops. Direct DNS/TCP failures do not rule out proxy access.')
    print('Default: connection checks + event probes for groups 6, 7 and 8, limit ONE per request. --metadata-only skips events.')
    print('RCA compares Python, Windows and the installed suite. Failed requests trigger bounded format/payload/timeout probes.')
    print('Each event request stays in your date range with limit=1; at most 18 event probes, no pagination. Overall checks: maximum 10 minutes.')
    root = args.suite_dir.resolve()
    report = dict(version=VERSION, utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  python='.'.join(map(str, sys.version_info[:3])), model_loaded=False,
                  event_requests=0, checks=[], ok=False)
    started = time.monotonic()
    try:
        cfg = configuration(root)
        cfg['suite_root'] = str(root)
        report['server_origin'] = cfg['url']
        print('Configured server: ' + cfg['url'])
        print('Settings parsed. TLS: Windows certificate store' if sys.platform == 'win32' else 'Settings parsed. TLS: default verified trust')
        print('Additional CA bundle: ' + ('configured' if cfg['ca_bundle'] else 'none'))
        print('Key source: ' + cfg['key_source'] + '; suite transport: ' + cfg['transport'])
        report['key_conflicts'] = cfg['key_conflicts']
        for name, found in cfg['key_conflicts'].items():
            print('  ' + name + ': ' + str(found))
        print('This utility tests direct requests; it does not require PyMISP startup.')
        if cfg['key_issue']:
            print('KEY_FORMAT: ' + cfg['key_issue'])
            report['key_issue'] = cfg['key_issue']
        for module in ('requests', 'truststore'):
            if (module == 'requests' or sys.platform == 'win32') and importlib.util.find_spec(module) is None:
                raise ConfigError('Missing ' + module + '. Run this from the installed suite Python environment.')
        selection = None
        if not args.metadata_only and not cfg['key_issue']:
            print('\nChoose the event date range. Event contents are read to count objects/attributes, then discarded.')
            selection = filters(args)
            report['filters'] = dict(start=selection[0], end=selection[1], groups=selection[2])
            print('Fetch: ' + ', '.join(str(group) for group in selection[2]) + ' | maximum 1 event per request | attachments disabled')
        deadline = time.monotonic() + 600
        def check(kind, label, probe_cfg, seconds=25):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                row = result('BUDGET', 'Overall ten-minute diagnostic budget reached; check not attempted.', step=kind, label=label, seconds=0)
                if kind == 'events':
                    row.update(group=probe_cfg['group'], variant=probe_cfg.get('event_variant', 'python'))
                return row
            row = run_step(kind, label, probe_cfg, min(seconds, remaining))
            if kind == 'events':
                row.update(group=probe_cfg['group'], variant=probe_cfg.get('event_variant', 'python'))
            return row
        report['checks'].append(check('inventory', 'Installed suite, runtime and recent fetch status', cfg, 15))
        for kind, label, timeout in [('proxy', 'Proxy/WPAD configuration', 10), ('dns', 'Local DNS', 10),
                                      ('routes', 'Windows routes and VPN indicators', 15), ('tcp', 'Direct TCP port', 12),
                                      ('certificate', 'Direct TLS issuer, validity and Windows trust', 20),
                                      ('login', 'TLS + login HTTPS (no key)', 25)]:
            report['checks'].append(check(kind, label, cfg, timeout))
        if not cfg['key_issue']:
            for kind, label in [('version', 'GET /servers/getVersion'), ('auth', 'GET /users/view/me'),
                                ('types', 'GET /attributes/describeTypes.json')]:
                report['checks'].append(check(kind, label, cfg))
            for kind, label in [('native_auth', 'Windows WinHTTP: GET /users/view/me'),
                                ('auth_anonymous', 'Anonymous comparison: GET /users/view/me'),
                                ('auth_json', 'Explicit JSON format: GET /users/view/me.json')]:
                report['checks'].append(check(kind, label, cfg, 30))
            if selection:
                report['checks'].append(check('sharing_groups', 'Sharing-group visibility (metadata only)', cfg))
                start, end, groups = selection
                for group in groups:
                    from connection_rca import followup_variants
                    def event_check(variant, seconds=30):
                        report['event_requests'] += 1
                        row = check('events', f'Group {group}: {variant} event fetch',
                                    dict(cfg, group=group, start=start, end=end, event_variant=variant), seconds)
                        report['checks'].append(row)
                        return row
                    baseline = event_check('python')
                    event_check('native')
                    event_check('suite', 70)
                    for variant in followup_variants(baseline):
                        event_check(variant, 70 if variant == 'long_timeout' else 30)
        api = [r for r in report['checks'] if r['step'] in ('version', 'auth', 'types', 'events')]
        required_api = [r for r in api if r.get('variant', 'python') in ('python', 'suite')]
        report['ok'] = bool(required_api) and not cfg['key_issue'] and all(r['code'] in ('PASS', 'EMPTY') for r in required_api)
        if any(r['step'] == 'auth_anonymous' and r['code'] == 'PASS' for r in report['checks']):
            report['ok'] = False
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
    discard_body_digests(report['checks'])
    report['event_summary'] = event_summary(report['checks'])
    report['summary'] = diagnosis(report['checks'])
    from connection_rca import analyse, text_report, screen_report
    report['rca'] = analyse(report)
    if report['event_summary']:
        print('\nEVENT FETCH RESULTS')
        for message in report['event_summary']:
            print('  ' + message)
    print('\nRESULT: ' + ('API CHECKS PASSED' if report['ok'] else 'CHECKS FAILED OR INCOMPLETE'))
    for message in report['summary']:
        print('  ' + message)
    print('\n' + screen_report(report))
    report['elapsed_seconds'] = round(time.monotonic() - started, 2)
    try:
        directory = root / 'setup'
        directory.mkdir(exist_ok=True)
        (directory / 'connection-doctor.json').write_text(json.dumps(report, indent=2), encoding='utf8')
        (directory / 'connection-rca.txt').write_text(text_report(report), encoding='utf8')
        print('Saved: setup/connection-doctor.json. All results are also on this screen.')
        print('Diagnosis and next actions: setup/connection-rca.txt')
    except OSError:
        print('Report could not be saved; results remain above.')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    mp.freeze_support()
    raise SystemExit(main())
