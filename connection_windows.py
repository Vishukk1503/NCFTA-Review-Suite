"""Read-only Windows network diagnostics; no shells, installs or system changes.

Native declarations follow Microsoft's WinSDK headers. Raw addresses, adapter
names, process lists, proxy URLs, credentials and certificate blobs stay local.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import hashlib
import socket
import struct
import sys

P = C.c_void_p
D = W.DWORD
B = W.BOOL
PD = C.POINTER(D)


class NativeError(Exception):
    def __init__(self, code):
        self.code = int(code)


def dll(name):
    return C.WinDLL(name, use_last_error=True)


def bind(lib, name, restype, args):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = restype, args
    return fn


def required(value):
    if not value:
        raise NativeError(C.get_last_error())
    return value


PRODUCTS = {
    'Zscaler': ('zscaler', 'zsatunnel', 'zsaservice'),
    'Netskope': ('netskope', 'stagent'),
    'GlobalProtect': ('globalprotect', 'pangpa', 'pangps', 'palo alto'),
    'Cisco VPN': ('anyconnect', 'cisco secure client', 'vpnagent', 'vpnui'),
    'Fortinet': ('forticlient', 'fortissl', 'fortinet'),
    'Ivanti/Pulse': ('ivanti', 'pulse secure', 'pulseui', 'pulse.exe'),
    'Check Point': ('check point', 'trac.exe', 'tracsrvwrapper'),
    'OpenVPN': ('openvpn', 'tap-windows'),
    'WireGuard': ('wireguard', 'wintun'),
    'Tailscale': ('tailscale',),
    'SonicWall': ('sonicwall', 'netextender'),
}


def product_markers(text):
    value = text.lower()
    return [name for name, needles in PRODUCTS.items() if any(n in value for n in needles)]


def proxy_config():
    class IEProxy(C.Structure):
        _fields_ = [('autodetect', B), ('pac', P), ('proxy', P), ('bypass', P)]
    class Proxy(C.Structure):
        _fields_ = [('access', D), ('proxy', P), ('bypass', P)]
    win = dll('winhttp')
    free = bind(dll('kernel32'), 'GlobalFree', P, [P])
    current = IEProxy()
    default = Proxy()
    info = {}
    get_user = bind(win, 'WinHttpGetIEProxyConfigForCurrentUser', B, [C.POINTER(IEProxy)])
    if get_user(C.byref(current)):
        info.update(windows_wpad=bool(current.autodetect), windows_connection_pac=bool(current.pac),
                    windows_connection_proxy=bool(current.proxy))
        for ptr in (current.pac, current.proxy, current.bypass):
            if ptr:
                free(ptr)
    else:
        info['windows_proxy_query_error'] = C.get_last_error()
    get_default = bind(win, 'WinHttpGetDefaultProxyConfiguration', B, [C.POINTER(Proxy)])
    if get_default(C.byref(default)):
        info['winhttp_machine_proxy'] = bool(default.proxy)
        for ptr in (default.proxy, default.bypass):
            if ptr:
                free(ptr)
    import winreg
    policies = {}
    for browser, path in (('Edge', r'Software\Policies\Microsoft\Edge'), ('Chrome', r'Software\Policies\Google\Chrome')):
        for hive, scope in ((winreg.HKEY_LOCAL_MACHINE, 'machine'), (winreg.HKEY_CURRENT_USER, 'user')):
            try:
                with winreg.OpenKey(hive, path) as reg:
                    entry = {}
                    for name in ('ProxyMode', 'ProxyServer', 'ProxyPacUrl'):
                        try:
                            value = winreg.QueryValueEx(reg, name)[0]
                        except OSError:
                            continue
                        if name == 'ProxyMode':
                            entry['mode'] = value if value in ('direct', 'auto_detect', 'pac_script', 'fixed_servers', 'system') else 'other'
                        else:
                            entry[name + '_configured'] = bool(value)
                    if entry:
                        policies[browser + '_' + scope] = entry
            except OSError:
                pass
    info['browser_proxy_policies'] = policies
    return info


def route_inventory(host, port):
    # Only the documented prefix through Ipv6IfIndex is dereferenced.
    class Adapter(C.Structure):
        pass
    Adapter._fields_ = [('length', D), ('index', D), ('next', C.POINTER(Adapter)),
                       ('adapter_name', P), ('unicast', P), ('anycast', P), ('multicast', P),
                       ('dns', P), ('suffix', P), ('description', W.LPWSTR), ('friendly', W.LPWSTR),
                       ('mac', C.c_ubyte * 8), ('mac_length', D), ('flags', D), ('mtu', D),
                       ('if_type', D), ('oper_status', D), ('ipv6_index', D)]
    lib = dll('iphlpapi')
    get_adapters = bind(lib, 'GetAdaptersAddresses', D, [D, D, P, P, PD])
    size = D(16384)
    for _ in range(3):
        buffer = C.create_string_buffer(size.value)
        code = get_adapters(0, 15, None, buffer, C.byref(size))
        if code != 111:
            break
    if code:
        raise NativeError(code)
    adapters = {}
    active = []
    ptr = C.cast(buffer, C.POINTER(Adapter))
    count = 0
    while ptr and count < 512:
        item = ptr.contents
        markers = product_markers((item.description or '') + ' ' + (item.friendly or ''))
        summary = dict(interface_type={6: 'ethernet', 23: 'PPP', 24: 'loopback', 71: 'Wi-Fi', 131: 'tunnel'}.get(item.if_type, 'other'),
                       up=item.oper_status == 1, product_markers=markers)
        if item.index:
            adapters[(socket.AF_INET, item.index)] = summary
        if item.ipv6_index:
            adapters[(socket.AF_INET6, item.ipv6_index)] = summary
        if summary['up'] and (markers or item.if_type in (23, 131)):
            active.append(summary)
        ptr = item.next
        count += 1
    best = bind(lib, 'GetBestInterfaceEx', D, [P, PD])
    routes = []
    seen = set()
    entries = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for family, _, _, _, address in entries:
        if family == socket.AF_INET:
            packed = struct.pack('<HH4s8s', family, 0, socket.inet_pton(family, address[0]), b'\0' * 8)
        elif family == socket.AF_INET6:
            packed = struct.pack('<HHI16sI', family, 0, 0, socket.inet_pton(family, address[0]), address[3])
        else:
            continue
        index = D()
        addr_buffer = C.create_string_buffer(packed)
        code = best(addr_buffer, C.byref(index))
        key = (family, index.value, code)
        if key in seen:
            continue
        seen.add(key)
        summary = dict(adapters.get((family, index.value), {}))
        summary['family'] = 'IPv4' if family == socket.AF_INET else 'IPv6'
        if code:
            summary['route_query_error'] = code
        routes.append(summary)
    running = set()
    process_check = False
    try:
        import psutil
        process_check = True
        for process in psutil.process_iter(['name']):
            try:
                running.update(product_markers(process.info.get('name') or ''))
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
    except ImportError:
        pass
    return dict(routes=routes, active_vpn_like_adapters=active,
                known_network_software_running=sorted(running), process_check_available=process_check,
                vpn_detection_limit='Adapter/software presence does not prove interception. Filter-based VPNs may use a physical interface.')


def safe_cert_name(value):
    return ''.join(c for c in value if c.isprintable() and c not in '\r\n\t')[:180]


def native_error(code):
    category, text = {
        12002: ('TIMEOUT', 'Windows HTTP request timed out.'),
        12007: ('DNS', 'Windows could not resolve the target or proxy.'),
        12029: ('CONNECTION', 'Windows could not establish the connection.'),
        12030: ('CONNECTION', 'Windows connection was reset or terminated.'),
        12037: ('TLS', 'Windows rejected the certificate validity dates.'),
        12038: ('TLS', 'Windows rejected the certificate hostname.'),
        12044: ('CLIENT_CERT', 'Server requested a client certificate; none was selected automatically.'),
        12045: ('TLS', 'Windows does not trust the certificate authority.'),
        12157: ('TLS', 'Windows secure-channel negotiation failed.'),
        12166: ('PROXY', 'Windows automatic proxy configuration script is invalid.'),
        12167: ('PROXY', 'Windows could not download the automatic proxy script.'),
        12175: ('TLS', 'Windows certificate/secure-channel validation failed.'),
        12180: ('PROXY', 'Windows automatic proxy discovery failed.'),
    }.get(code, ('WINDOWS_HTTP', 'Windows HTTP operation failed; see the numeric Windows error.'))
    return dict(code=category, message=text, windows_error=code)


def winhttp_get(cfg, path='/users/view/me'):
    """Authenticated metadata GET, native configured proxy, verified TLS.

    Redirects, cookies and automatic Windows-account authentication are disabled.
    No certificate flags are relaxed; extra PEM roots are not imported to Windows.
    """
    from types import SimpleNamespace
    from connection_doctor import response_facts, api_result, http_error, result, MAX_RESPONSE
    import json
    if path not in ('/users/view/me', '/users/view/me.json'):
        return result('REFUSED', 'Native diagnostic endpoint is not allowed.')
    win = dll('winhttp')
    open_session = bind(win, 'WinHttpOpen', P, [W.LPCWSTR, D, W.LPCWSTR, W.LPCWSTR, D])
    connect = bind(win, 'WinHttpConnect', P, [P, W.LPCWSTR, W.WORD, D])
    open_request = bind(win, 'WinHttpOpenRequest', P, [P, W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, P, D])
    set_option = bind(win, 'WinHttpSetOption', B, [P, D, P, D])
    timeouts = bind(win, 'WinHttpSetTimeouts', B, [P, C.c_int, C.c_int, C.c_int, C.c_int])
    send = bind(win, 'WinHttpSendRequest', B, [P, W.LPCWSTR, D, P, D, D, C.c_size_t])
    receive = bind(win, 'WinHttpReceiveResponse', B, [P, P])
    query = bind(win, 'WinHttpQueryHeaders', B, [P, D, W.LPCWSTR, P, PD, PD])
    query_option = bind(win, 'WinHttpQueryOption', B, [P, D, P, PD])
    read = bind(win, 'WinHttpReadData', B, [P, P, D, PD])
    close = bind(win, 'WinHttpCloseHandle', B, [P])
    handles = []
    try:
        session = required(open_session('NCFTA-Connection-Doctor/1.2', 4, None, None, 0))
        handles.append(session)
        required(timeouts(session, 5000, 5000, 10000, 15000))
        connection = required(connect(session, cfg['host'], cfg['port'], 0))
        handles.append(connection)
        request = required(open_request(connection, 'GET', path, None, None, None, 0x00800000))
        handles.append(request)
        disabled = D(1 | 2 | 4)  # cookies, redirects, automatic authentication
        required(set_option(request, 63, C.byref(disabled), C.sizeof(disabled)))
        decompress = D(3)  # gzip and deflate only
        required(set_option(request, 118, C.byref(decompress), C.sizeof(decompress)))
        headers = 'Accept: application/json\r\nContent-Type: application/json\r\nAuthorization: ' + cfg['key'] + '\r\n'
        required(send(request, headers, len(headers), None, 0, 0, 0))
        del headers
        required(receive(request, None))
        status, size = D(), D(C.sizeof(D))
        required(query(request, 19 | 0x20000000, None, C.byref(status), C.byref(size), None))
        def header(name):
            length = D()
            query(request, 65535, name, None, C.byref(length), None)
            if not length.value or length.value > 65536:
                return ''
            buffer = C.create_unicode_buffer(length.value // 2 + 1)
            if query(request, 65535, name, buffer, C.byref(length), None):
                return buffer.value
            return ''
        selected_headers = {name: header(name) for name in ('Content-Type', 'Content-Encoding', 'Server', 'Via', 'Location', 'WWW-Authenticate')}
        cert_info = {}
        cert = P()
        size = D(C.sizeof(cert))
        if query_option(request, 78, C.byref(cert), C.byref(size)) and cert:
            crypt = dll('crypt32')
            get_name = bind(crypt, 'CertGetNameStringW', D, [P, D, D, P, W.LPWSTR, D])
            free_cert = bind(crypt, 'CertFreeCertificateContext', B, [P])
            class CertContext(C.Structure):
                _fields_ = [('encoding', D), ('data', P), ('size', D), ('info', P), ('store', P)]
            try:
                length = get_name(cert, 4, 1, None, None, 0)
                if 0 < length < 4096:
                    name = C.create_unicode_buffer(length)
                    get_name(cert, 4, 1, None, name, length)
                    cert_info['certificate_issuer'] = safe_cert_name(name.value)
                context = C.cast(cert, C.POINTER(CertContext)).contents
                if 0 < context.size < 1048576:
                    cert_info['certificate_sha256'] = hashlib.sha256(C.string_at(context.data, context.size)).hexdigest()
            finally:
                free_cert(cert)
        data = bytearray()
        while True:
            buffer = C.create_string_buffer(65536)
            received = D()
            required(read(request, buffer, len(buffer), C.byref(received)))
            if not received.value:
                break
            data.extend(buffer.raw[:received.value])
            if len(data) > MAX_RESPONSE:
                return result('SIZE_LIMIT', 'Windows HTTP response exceeded the diagnostic size limit.')
        response = SimpleNamespace(status_code=status.value, headers=selected_headers)
        facts = response_facts(response, data)
        facts.update(cert_info)
        facts['native_proxy_policy'] = 'Windows system/user automatic configuration'
        facts['native_trust_policy'] = 'Windows certificate store; extra PEM not imported'
        if status.value != 200:
            row = http_error(status.value)
        else:
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeError):
                row = result('NOT_JSON', 'Windows HTTP also received a non-JSON response.')
            else:
                row = api_result('auth', payload)
                facts['body_kind'] = 'json'
                facts.pop('_body_digest', None)
        row.update(facts)
        return row
    except NativeError as exc:
        return native_error(exc.code)
    finally:
        for handle in reversed(handles):
            close(handle)
