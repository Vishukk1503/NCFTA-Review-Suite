"""Bounded evidence collection and diagnosis; no model imports or secret output."""
from __future__ import annotations

import ast
import contextlib
import datetime as dt
from email.utils import parsedate_to_datetime
import hashlib
import importlib
import importlib.metadata
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import urlsplit
from unittest.mock import patch


NETWORK_REFERENCE = {
    'suite.py': '51dfb0c9dabbbe4119a0583eda04d291a44aca7398b492b31271a3110fe177ef',
    'ncfta_client.py': '8eecb1f91b22fa2de00fa9c5d814a3f49b6312bcc858a160777ca8ccdab5cbde',
    'tls_support.py': '588e3659625462878c6ae1269dde041b9caa774d5063df6c6670fa2c2ea4f2a1',
    'Start-NCFTA.bat': 'dac4128eaebb27ef0233942205d7a40fc35186b73d903391f21e64e8779c0eae',
}


def correlation_facts(headers):
    facts = {}
    ids = {}
    for name, pattern in (('CF-Ray', r'[0-9a-fA-F]{16,32}(?:-[A-Z0-9]{3,10})?'),
                          ('X-Request-ID', r'(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})')):
        value = str(headers.get(name, ''))
        if re.fullmatch(pattern, value):
            ids[name] = value
    if ids:
        facts['correlation_ids'] = ids
    try:
        server = parsedate_to_datetime(str(headers.get('Date', '')))
        facts['server_clock_delta_seconds'] = round((server - dt.datetime.now(dt.timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        pass
    retry = str(headers.get('Retry-After', ''))
    if retry.isdecimal() and len(retry) <= 5:
        facts['retry_after_seconds'] = int(retry)
    return facts


def recent_states(runs):
    states = []
    if not runs.is_dir():
        return states
    folders = sorted((p for p in runs.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    for index, folder in enumerate(folders, 1):
        state = dict(recency=index)
        for filename in ('manifest.json', 'status.json'):
            path = folder / filename
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            try:
                obj = json.loads(path.read_text(encoding='utf-8-sig'))
                for field in ('fetch_complete', 'inference_complete'):
                    if isinstance(obj.get(field), bool):
                        state[field] = obj[field]
                if obj.get('phase') in ('Fetching', 'Preparing', 'Fetch complete', 'Loading v3', 'Extracting', 'Complete', 'exported', 'extracted', 'extracting'):
                    state['phase'] = obj['phase']
                if obj.get('transport') in ('requests', 'pymisp'):
                    state['transport'] = obj['transport']
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        path = folder / 'run.log'
        retry_counts = {str(g): 0 for g in (6, 7, 8)}
        if path.is_file():
            try:
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 65536))
                    tail = stream.read(65536).decode('utf-8', errors='replace')
                for line in tail.splitlines():
                    try:
                        row = json.loads(line)
                        if row.get('code') == 'fetch_retry' and str(row.get('group')) in retry_counts:
                            retry_counts[str(row['group'])] += 1
                    except (ValueError, TypeError, AttributeError):
                        continue
            except OSError:
                pass
        state['fetch_retries_in_last_64kb'] = retry_counts
        states.append(state)
    return states


def inventory(cfg):
    from connection_doctor import result
    root = Path(cfg['suite_root'])
    versions = {}
    for name in ('requests', 'urllib3', 'certifi', 'truststore', 'pymisp', 'psutil'):
        try:
            value = importlib.metadata.version(name)
            versions[name] = value if re.fullmatch(r'[A-Za-z0-9.+_-]{1,60}', value) else 'unrecognized'
        except importlib.metadata.PackageNotFoundError:
            versions[name] = 'missing'
    files = {}
    for name, expected in NETWORK_REFERENCE.items():
        path = root / name
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            files[name] = dict(state='missing-or-oversize')
        else:
            digest = hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
            files[name] = dict(state='matches-current' if digest == expected else 'different-version', sha256=digest)
    settings_path = root / ('settings.json' if (root / 'settings.json').is_file() else 'settings.example.json')
    settings = json.loads(settings_path.read_text(encoding='utf-8-sig'))
    runs = Path(os.path.expandvars(settings.get('runs', 'runs'))).expanduser()
    if not runs.is_absolute():
        runs = root / runs
    existing = runs
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    storage = {}
    try:
        storage['free_mb'] = round(shutil.disk_usage(existing).free / 1024 ** 2)
        storage['below_suite_512mb_guard'] = storage['free_mb'] < 512
        with tempfile.TemporaryFile(dir=existing) as stream:
            stream.write(b'NCFTA diagnostic write check')
            stream.flush()
        storage['writable'] = True
    except OSError:
        storage['writable'] = False
    expected_python = os.environ.get('NCFTA_PYTHON') or str(root / '.venv' / 'Scripts' / 'python.exe')
    return result('INFO', 'Installed network code, package versions, launcher runtime and recent fetch stages inspected.',
                  python_version='.'.join(map(str, sys.version_info[:3])), packages=versions,
                  suite_runtime_matches=Path(expected_python).resolve() == Path(sys.executable).resolve(),
                  network_files=files, output_storage=storage, recent_run_states=recent_states(runs))


def followup_variants(baseline):
    code = baseline['code']
    if code in ('NOT_JSON', 'SHAPE', 'DECOMPRESSION', 'BODY_INTERRUPTED', 'SIZE_LIMIT'):
        return ['json_endpoint', 'identity', 'metadata']
    if code in ('TIMEOUT', 'CONNECT_TIMEOUT', 'READ_TIMEOUT', 'DEADLINE', 'CONNECTION'):
        return ['metadata', 'long_timeout']
    # A denied request is not retried with alternative encodings or URL spellings.
    return []


class ProbeStop(Exception):
    def __init__(self, row):
        self.row = row


def suite_namespace(root):
    """Use only the installed network/settings definitions, never suite imports/model."""
    tree = ast.parse((root / 'suite.py').read_text(encoding='utf-8-sig'))
    names = {'SuiteError', 'read', 'settings', 'credentials', 'transport', 'HTTPClient'}
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    if not names <= {node.name for node in selected}:
        raise ValueError('Unsupported installed suite definitions')
    namespace = dict(__name__='ncfta_installed_network_probe', HERE=root, Path=Path, os=os, json=json,
                     importlib=importlib, hashlib=hashlib)
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'installed-suite-network', 'exec'), namespace)
    return namespace


def suite_event_probe(cfg):
    from connection_doctor import api_result, result, response_facts, event_body, network_error, http_error, MAX_RESPONSE
    import requests
    root = Path(cfg['suite_root'])
    trace = []
    post_count = 0
    original_send = requests.Session.send
    origin = (urlsplit(cfg['url']).scheme, urlsplit(cfg['url']).netloc)
    allowed_get = {'/servers/getVersion', '/servers/getPyMISPVersion.json', '/users/view/me',
                   '/users/view/me.json', '/users/view/me/1', '/attributes/describeTypes.json'}
    def guarded_send(session, request, **kwargs):
        nonlocal post_count
        parsed = urlsplit(request.url)
        is_event = request.method == 'POST' and parsed.path.rstrip('/') == '/events/restSearch'
        if ((parsed.scheme, parsed.netloc) != origin or parsed.query or parsed.fragment
                or not (is_event or request.method == 'GET' and parsed.path in allowed_get)
                or len(trace) >= 10):
            raise ProbeStop(result('REFUSED', 'Installed client attempted a request outside the diagnostic allowlist.'))
        entry = dict(method=request.method, endpoint=parsed.path, stage='prepared')
        trace.append(entry)
        if not kwargs.get('verify', True):
            raise ProbeStop(result('TLS_DISABLED', 'Installed client disabled TLS verification; request stopped.'))
        if request.headers.get('Authorization') != cfg['key']:
            raise ProbeStop(result('KEY_REPLACED', 'Installed client did not retain the same configured API key; request stopped.'))
        if is_event:
            post_count += 1
            body = json.loads(request.body)
            event_body(cfg)  # validate scope before allowing any installed-client POST
            if (post_count > 1 or body.get('returnFormat') != 'json'
                    or [str(g) for g in body.get('sharinggroup', [])] != [str(cfg['group'])]
                    or body.get('from') != cfg['start'] or body.get('to') != cfg['end']
                    or str(body.get('limit')) != '1' or str(body.get('page')) != '1'
                    or body.get('metadata') not in (False, 0, '0')
                    or body.get('withAttachments') not in (False, 0, '0')):
                raise ProbeStop(result('REQUEST_MISMATCH', 'Installed suite request changed date/group/limit/attachment filters; request stopped.'))
            entry['request_scope_matches'] = True
        # Keep the installed headers/body/TLS behavior, but impose diagnostic bounds.
        kwargs.update(allow_redirects=False, stream=True, timeout=(5, 60))
        start = time.monotonic()
        try:
            response = original_send(session, request, **kwargs)
        except requests.exceptions.RequestException as exc:
            entry.update(network_error(exc, requests), seconds=round(time.monotonic() - start, 2))
            raise
        data = bytearray()
        try:
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    raise ProbeStop(result('SIZE_LIMIT', 'Installed suite response exceeded the diagnostic 8 MB limit.'))
        finally:
            response.close()
        facts = response_facts(response, data, cfg['key'])
        facts.pop('_body_digest', None)
        entry.update(facts, seconds=round(time.monotonic() - start, 2), stage='response')
        if response.status_code != 200:
            raise ProbeStop(dict(http_error(response.status_code), **facts))
        try:
            json.loads(data)
        except (ValueError, UnicodeError):
            raise ProbeStop(result('NOT_JSON', 'Installed suite received non-JSON from this endpoint.', **facts))
        entry['body_kind'] = 'json'
        response._content = bytes(data)
        response._content_consumed = True
        return response
    previous_logging = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            namespace = suite_namespace(root)
            settings = namespace['settings']()
            if settings['url'] != cfg['url'] or namespace['credentials'](settings) != cfg['key']:
                return result('CONFIG_MISMATCH', 'Installed suite resolves a different URL/key than the doctor; no request sent.')
            mode = namespace['transport'](settings)
            sys.path.insert(0, str(root))
            # Workers are isolated; remove any previously imported helper before selecting the installed copy.
            for name in ('tls_support', 'ncfta_client'):
                sys.modules.pop(name, None)
            with patch.object(requests.Session, 'send', guarded_send):
                if mode == 'pymisp':
                    connector = importlib.import_module('ncfta_client')
                    context = connector.connect(settings['url'], cfg['key'], 60, settings.get('ca_bundle'))
                else:
                    context = namespace['HTTPClient'](settings['url'], cfg['key'], settings.get('ca_bundle'))
                with context as client:
                    payload = client.search(controller='events', return_format='json', sharinggroup=[cfg['group']],
                                            date_from=cfg['start'], date_to=cfg['end'], page=1, limit=1,
                                            metadata=False, with_attachments=False, pythonify=False)
                row = api_result('events', payload, cfg['group'], cfg['start'], cfg['end'])
                row['suite_transport'] = mode
    except ProbeStop as exc:
        row = exc.row
    except (ImportError, FileNotFoundError):
        row = result('DEPENDENCY', 'Installed suite network file/package is missing; see inventory.')
    except Exception as exc:
        # Follow wrapped causes without rendering any exception strings.
        pending, seen, recovered = [exc], set(), None
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            if isinstance(item, ProbeStop):
                recovered = item.row
                break
            if isinstance(item, requests.exceptions.RequestException):
                recovered = network_error(item, requests)
            if type(item).__name__ == 'NCFTAClientError':
                match = re.fullmatch(r'MISP request failed: HTTP ([1-5][0-9]{2})\. Check permissions and server availability\.', str(item))
                if match:
                    recovered = http_error(int(match[1]))
            pending.extend(x for x in (item.__cause__, item.__context__) if x is not None)
        row = recovered or result('SUITE_CLIENT', 'Installed suite connection code failed; compare its trace with standalone/native probes.')
    finally:
        logging.disable(previous_logging)
    row['suite_trace'] = trace
    row['suite_probe_bounds'] = 'Installed network definitions only; one event POST; timeout 5/60s; 8 MB; no redirects.'
    return row


def analyse(report):
    rows = report['checks']
    findings = []
    def add(code, confidence, evidence, action):
        findings.append(dict(code=code, confidence=confidence, evidence=evidence, next_action=action))
    inventory_row = next((r for r in rows if r['step'] == 'inventory'), {})
    if inventory_row.get('suite_runtime_matches') is False:
        add('RUNTIME_MISMATCH', 'confirmed', 'Doctor and suite launcher select different Python runtimes.', 'Use the installed suite BAT/runtime consistently; compare the package inventory before changing network settings.')
    storage = inventory_row.get('output_storage', {})
    if storage.get('writable') is False or storage.get('below_suite_512mb_guard'):
        add('OUTPUT_STORAGE', 'confirmed', 'Output folder is not writable or free space is below the suite 512 MB minimum.', 'Restore write access/free space on the configured output drive; this is separate from API access.')
    groups = {}
    for group in report.get('filters', {}).get('groups', []):
        cases = {r.get('variant', 'python'): r for r in rows if r['step'] == 'events' and r.get('group') == group}
        groups[str(group)] = {name: row['code'] for name, row in cases.items()}
        standard, native, suite = (cases.get(name, {}) for name in ('python', 'native', 'suite'))
        good = lambda row: row.get('code') in ('PASS', 'EMPTY')
        if good(standard) and not good(suite):
            add(f'GROUP_{group}_SUITE_DIFFERENCE', 'confirmed', f"Standalone Python={standard.get('code')}; installed suite={suite.get('code', 'missing')}.", 'Inspect suite_trace and the installed-file/runtime comparison. Repair the suite connection/configuration indicated there; standalone API access already works.')
        if not good(standard) and good(native):
            add(f'GROUP_{group}_WINDOWS_PATH_WORKS', 'confirmed', f"Python={standard.get('code')}; Windows={native.get('code')} for the same scoped search.", 'Use the approved Windows networking path or align Python proxy/trust behavior. This comparison alone does not identify a particular VPN as the cause.')
        if good(standard) and not good(native):
            add(f'GROUP_{group}_PYTHON_PATH_WORKS', 'confirmed', f"Python={standard.get('code')}; Windows={native.get('code')}.", 'The Python event path works; keep it. A native-only failure does not explain a successful Python fetch.')
        for variant, explanation in (('json_endpoint', 'Explicit JSON endpoint'), ('identity', 'Uncompressed response request'),
                                     ('metadata', 'Metadata-only event payload'), ('long_timeout', 'Longer response timeout')):
            if not good(standard) and good(cases.get(variant, {})):
                add(f'GROUP_{group}_{variant.upper()}_WORKS', 'observed difference', f'{explanation} succeeded after the standard event request failed.',
                    'Compare HTTP codes, response sizes and timings in the report. This isolated difference is a lead; sequential requests do not exclude a transient server/network change.')
        if cases and all(r.get('code') == 'EMPTY' for name, r in cases.items() if name in ('python', 'native', 'suite')):
            add(f'GROUP_{group}_EMPTY', 'confirmed result; cause unresolved', 'All tested clients returned zero visible matching events.', 'Do not treat this as a broken connection. An NCFTA administrator must distinguish no matching data from visibility restrictions for these dates.')
        failed = [r for r in cases.values() if r.get('code') not in ('PASS', 'EMPTY', 'SKIP')]
        if failed and not any(good(r) for r in cases.values()):
            codes = sorted({r['code'] for r in failed})
            add(f'GROUP_{group}_NO_WORKING_PATH', 'root cause unconfirmed', 'No tested event path succeeded; observed codes: ' + ', '.join(codes) + '.',
                'Use the included UTC time, endpoint, HTTP code and correlation IDs with NCFTA/network logs. Client evidence cannot identify an opaque server/gateway denial or timeout by itself.')
    api = [r for r in rows if r['step'] in ('version', 'auth', 'types', 'events')]
    codes = {r['code'] for r in api}
    markers = {marker for row in api for marker in row.get('page_markers', [])}
    if markers & {'access_block_message', 'browser_challenge', 'identity_sign_in'}:
        add('API_PAGE_INTERCEPTION_MARKER', 'response signature; policy owner unconfirmed',
            'Authenticated API responses contained: ' + ', '.join(sorted(markers)) + '.',
            'Use the recorded endpoint, UTC and correlation ID with the service/network administrator to resolve the approved API route or policy. Do not disable TLS or bypass the challenge.')
    elif 'misp_login_form' in markers:
        add('API_RETURNED_LOGIN_FORM', 'response signature', 'An authenticated API response contained a MISP login form.',
            'Compare the same-run authenticated user result, key-header preservation and suite trace. Investigate authentication/routing; a login form alone does not prove the key is invalid.')
    deltas = [abs(row['server_clock_delta_seconds']) for row in rows if 'server_clock_delta_seconds' in row]
    if len(deltas) >= 2 and all(delta > 300 for delta in deltas):
        add('SERVER_CLOCK_DIFFERENCE', 'observed difference', 'At least two server Date headers differ from local UTC by over five minutes.',
            'Have IT check Windows time synchronization and server/gateway clocks. The utility does not change the clock.')
    if 'KEY_REPLACED' in codes or 'CONFIG_MISMATCH' in codes:
        add('KEY_OR_CONFIG_DIFFERENCE', 'confirmed', 'A key/header/config comparison failed.', 'Correct the local environment/.env precedence or implicit authentication indicated in the report. Do not rotate a working key merely because another probe failed.')
    if 'TLS' in codes or 'TLS_DISABLED' in codes:
        add('TLS_PATH_FAILURE', 'confirmed failure stage', 'At least one authenticated path failed certificate verification or attempted to disable it.', 'Use the Windows trust or IT-approved CA path that passed; compare issuers and installed TLS helper. Keep certificate checks enabled.')
    if 'HTTP_429' in codes:
        add('RATE_LIMIT', 'confirmed HTTP response', 'A server/gateway returned HTTP 429.', 'Respect Retry-After where present; no additional retries were made for that denied probe.')
    if any(r.get('code') == 'BUDGET' for r in rows):
        add('DIAGNOSTIC_DEADLINE', 'confirmed', 'The overall ten-minute budget was reached.', 'The report is partial; use the completed timeout/endpoint evidence for server/network investigation.')
    if not groups:
        add('EVENTS_UNTESTED', 'confirmed limitation', 'No event comparison was completed.', 'Read the configuration/error result. Metadata-only mode deliberately omits event tests.')
    if report.get('error') or report.get('key_issue'):
        add('LOCAL_CHECK_INCOMPLETE', 'confirmed', report.get('error') or report['key_issue'], 'Resolve this local error before interpreting missing network results.')
    all_core_good = bool(groups) and all(all(cases.get(v) in ('PASS', 'EMPTY') for v in ('python', 'suite')) for cases in groups.values())
    if all_core_good:
        add('SCOPED_FETCH_WORKS', 'confirmed for tested scope', 'Standalone Python and the installed suite completed the selected group/date searches.',
            'Run the suite with these dates. This excludes a persistent one-event connection failure now; it does not validate ten-event pages, later pagination, model inference or Excel export.')
    return dict(status='SCOPED_FETCH_WORKS' if all_core_good else 'FAILURE_OR_INCOMPLETE', groups=groups, findings=findings,
                limits='No VPN/security settings changed. No bypass, test-data access or model loading. Sequential comparisons cannot prove the cause of a past/transient failure; server logs may still be required.')


def text_report(report):
    rca = report['rca']
    lines = ['NCFTA ROOT-CAUSE REPORT ' + report['version'], 'UTC: ' + report['utc'], 'Outcome: ' + rca['status']]
    for group, variants in rca['groups'].items():
        lines.append('Group ' + group + ': ' + ' | '.join(k + '=' + v for k, v in variants.items()))
    for finding in rca['findings']:
        lines.extend(['', finding['code'] + ' [' + finding['confidence'] + ']', finding['evidence'], 'Next: ' + finding['next_action']])
    lines.extend(['', 'Request evidence for service/network support:'])
    for row in report['checks']:
        if row.get('http_status') or row.get('code') in ('DEADLINE', 'READ_TIMEOUT', 'TIMEOUT', 'TLS', 'CONNECTION', 'CONNECT_TIMEOUT'):
            lines.append(row['label'] + ': ' + row['code'] + ' | ' + str(row.get('seconds', '?')) + 's | ' + json.dumps(row.get('correlation_ids', {})))
        for trace in row.get('suite_trace', []):
            lines.append('  Suite ' + trace['method'] + ' ' + trace['endpoint'] + ': ' + str(trace.get('http_status', trace.get('code', trace['stage']))) + ' | ' + json.dumps(trace.get('correlation_ids', {})))
    lines.extend(['', rca['limits']])
    return '\n'.join(lines)


def screen_report(report):
    rca = report['rca']
    lines = ['FINAL DIAGNOSIS: ' + rca['status']]
    for group, variants in rca['groups'].items():
        lines.append('Group ' + group + ': ' + ' | '.join(k + '=' + v for k, v in variants.items()))
    for finding in rca['findings'][:6]:
        lines.extend([finding['code'] + ' [' + finding['confidence'] + ']: ' + finding['evidence'],
                      '  Next: ' + finding['next_action']])
    if len(rca['findings']) > 6:
        lines.append('Additional findings are included in setup/connection-rca.txt.')
    return '\n'.join(lines)
