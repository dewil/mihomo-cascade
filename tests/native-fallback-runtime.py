#!/usr/bin/env python3
"""Isolated native Mihomo fallback experiment. No credentials or external traffic.

Run: python3 tests/native-fallback-runtime.py [--mihomo /path/to/mihomo]
JSON evidence goes to stdout. Temporary runtime and child process are cleaned.
SOCKS exits only accept the fixture HTTP endpoint; faults close connections.
Samples include traffic-triggered health checks: timing is not a failover SLA.
The all-dead cases intentionally produce HTTP 502 or timeout. CLI success means
the experiment completed; inspect evidence for behavioral conclusions.
"""
import argparse
import http.client
import http.server
import json
import pathlib
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request


class Endpoint(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (self.headers.get('X-Fixture-Exit', 'unknown') + '\n').encode()
        self.send_response(204 if self.path == '/health' else 200)
        self.send_header('Content-Length', '0' if self.path == '/health' else str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        if self.path != '/health':
            self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(204)
        self.send_header('Connection', 'close')
        self.end_headers()

    def log_message(self, *_):
        pass


def read_exact(sock, size):
    result = b''
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise EOFError()
        result += part
    return result


class Exit(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            sock = self.request
            sock.settimeout(2)
            if self.server.state == 'down':
                return
            version, count = read_exact(sock, 2)
            assert version == 5
            read_exact(sock, count)
            sock.sendall(b'\x05\x00')
            version, command, _, atyp = read_exact(sock, 4)
            assert version == 5 and command == 1 and atyp == 1
            host = socket.inet_ntoa(read_exact(sock, 4))
            port = struct.unpack('!H', read_exact(sock, 2))[0]
            assert (host, port) == self.server.endpoint
            sock.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
            request = b''
            while b'\r\n\r\n' not in request:
                request += read_exact(sock, 1)
                assert len(request) < 65536
            if self.server.state == 'health-fails' and request.split(b' ', 2)[1] == b'/health':
                return
            request = request.replace(b'\r\n\r\n', b'\r\nX-Fixture-Exit: ' + self.server.name.encode() + b'\r\n\r\n', 1)
            with socket.create_connection(self.server.endpoint, timeout=2) as upstream:
                upstream.sendall(request)
                while True:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        break
                    sock.sendall(chunk)
        except (OSError, EOFError, AssertionError):
            return


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mihomo', default='/usr/local/bin/mihomo')
    args = parser.parse_args()
    version = subprocess.check_output([args.mihomo, '-v'], text=True).strip()
    result = {'version': version, 'interval_seconds': 1, 'timeout_ms': 500,
              'limitations': 'Synthetic loopback SOCKS exits, HTTP target, new connections only; not production acceptance.',
              'snapshots': []}
    servers = []
    started_servers = []
    proc = None
    try:
        endpoint = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        servers.append(endpoint)
        for name in ('DE', 'PL'):
            server = Server(('127.0.0.1', 0), Exit)
            server.name, server.state, server.endpoint = name, 'up', endpoint.server_address
            servers.append(server)
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            started_servers.append(server)
        mixed, controller = free_port(), free_port()
        while controller == mixed:
            controller = free_port()
        url = 'http://127.0.0.1:%s' % endpoint.server_port
        config = {'mixed-port': mixed, 'bind-address': '127.0.0.1', 'allow-lan': False,
                  'external-controller': f'127.0.0.1:{controller}', 'mode': 'rule', 'log-level': 'warning',
                  'dns': {'enable': False}, 'profile': {'store-selected': False},
                  'proxies': [{'name': s.name, 'type': 'socks5', 'server': '127.0.0.1', 'port': s.server_address[1]} for s in servers[1:]],
                  'proxy-groups': [{'name': 'egress', 'type': 'fallback', 'proxies': ['DE', 'PL'],
                                    'url': url + '/health', 'interval': 1, 'timeout': 500, 'lazy': False}],
                  'rules': ['MATCH,egress']}
        api = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def get_proxy(name):
            with api.open(f'http://127.0.0.1:{controller}/proxies/{name}', timeout=2) as response:
                return json.load(response)

        def traffic():
            conn = http.client.HTTPConnection('127.0.0.1', mixed, timeout=3)
            try:
                conn.request('GET', url + '/traffic', headers={'Connection': 'close'})
                response = conn.getresponse()
                return {'status': response.status, 'exit': response.read().decode().strip()}
            except (OSError, http.client.HTTPException) as exc:
                return {'error': type(exc).__name__}
            finally:
                conn.close()

        def direct_traffic(server):
            try:
                with socket.create_connection(server.server_address, timeout=2) as sock:
                    sock.sendall(b'\x05\x01\x00')
                    assert read_exact(sock, 2) == b'\x05\x00'
                    sock.sendall(b'\x05\x01\x00\x01' + socket.inet_aton('127.0.0.1') + struct.pack('!H', endpoint.server_port))
                    assert read_exact(sock, 10)[1] == 0
                    sock.sendall(b'GET /traffic HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                    response = http.client.HTTPResponse(sock)
                    response.begin()
                    return {'status': response.status, 'exit': response.read().decode().strip()}
            except (OSError, EOFError, AssertionError, http.client.HTTPException) as exc:
                return {'error': type(exc).__name__}

        with tempfile.TemporaryDirectory(prefix='mihomo-fallback-runtime-') as runtime:
            path = pathlib.Path(runtime)
            (path / 'config.json').write_text(json.dumps(config))
            with (path / 'runtime.log').open('w+') as log:
                proc = subprocess.Popen([args.mihomo, '-d', runtime, '-f', str(path / 'config.json')], stdout=log, stderr=subprocess.STDOUT)
                try:
                    for _ in range(50):
                        if proc.poll() is not None:
                            raise RuntimeError('Mihomo exited during startup')
                        try:
                            get_proxy('egress')
                            break
                        except OSError:
                            time.sleep(.1)
                    else:
                        raise RuntimeError('Controller did not become ready')
                    for label, de, pl in [('normal', 'up', 'up'), ('primary-down', 'down', 'up'),
                                          ('both-down', 'down', 'down'), ('recovery', 'up', 'up'),
                                          ('health-fails-traffic-works', 'health-fails', 'health-fails'),
                                          ('DE-down-PL-health-fails-traffic-works', 'down', 'health-fails')]:
                        servers[1].state, servers[2].state = de, pl
                        start = time.monotonic()
                        for delay in (0, .5, 1.5, 3):
                            time.sleep(max(0, start + delay - time.monotonic()))
                            state = {n: get_proxy(n) for n in ('egress', 'DE', 'PL')}
                            result['snapshots'].append({'case': label, 'seconds': round(time.monotonic() - start, 3),
                                                        'selected': state['egress'].get('now'),
                                                        'members': {n: {k: state[n].get(k) for k in ('alive', 'history', 'extra')} for n in ('DE', 'PL')},
                                                        'traffic': traffic(),
                                                        'direct_controls': {s.name: direct_traffic(s) for s in servers[1:]} if delay == 3 else None})
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    log.flush()
                    log.seek(0)
                    result['runtime_log'] = log.read()
                    proc = None
    finally:
        if proc is not None:
            proc.kill()
            proc.wait(timeout=5)
        for server in started_servers:
            server.shutdown()
        for server in servers:
            server.server_close()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
