"""Local real Consul checks transport. Credentials exist only in protected runtime/RAM."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from consul_pilot import decode_json, evaluate, ID

PIN = '9f80affeb492d5e2d8d6c8626107666923e1935ec41c1a3fc6ebcce8846368ec'
VERSION = '2.0.4'


def protected_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(data)


def verify_binary(binary):
    p = Path(binary)
    if not p.is_absolute() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != PIN:
        raise ValueError('CONSUL_DEPENDENCY')


def prepare_runtime(runtime_dir):
    path = Path(runtime_dir)
    if not path.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('UNSAFE_RUNTIME')
    resolved = path.resolve()
    # Runtime is deliberately restricted to nonsynced OS temporary storage.
    if not resolved.is_relative_to('/tmp'):
        raise ValueError('UNSAFE_RUNTIME')
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError('RUNTIME_NOT_EMPTY')
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    path.chmod(0o700)
    return path


class ConsulBackend:
    def __init__(self, binary, runtime_dir, observer_ids, ttl_seconds=5):
        verify_binary(binary)
        if not observer_ids or len(set(observer_ids)) != len(observer_ids) or any(type(x) is not str or not ID.fullmatch(x) for x in observer_ids):
            raise ValueError('INVALID_OBSERVERS')
        self.binary, self.runtime = binary, prepare_runtime(runtime_dir)
        self.ids, self.ttl = list(observer_ids), ttl_seconds
        self.processes, self.files, self.tokens, self.ports, self.nodes = [], [], {}, {}, {}
        self.pidfds = {}
        self.registered = set()
        self.prefix = 'pilot-' + secrets.token_hex(5)
        self.management = str(uuid.uuid4())
        self.reader = None
        self.metrics = dict(version=VERSION, binary_sha256=PIN, api_reads=0, api_writes=0, ttl_expiration_observed=False, acl_denials={})
        self.guard = self.guard_socket = None
        self.started_at = time.monotonic()
        self.resource_samples = []

    def _file(self, name, value):
        path = self.runtime / name
        protected_write(path, value)
        self.files.append(path)
        return path

    def _request(self, node, method, path, body=None, token=None):
        self.metrics['api_reads' if method == 'GET' else 'api_writes'] += 1
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Consul-Token'] = token
        req = urllib.request.Request('http://127.0.0.1:%d%s' % (self.ports[node]['http'], path), data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=2) as response:
                raw = response.read(1024 * 1024 + 1)
                return response.status, dict(response.headers), decode_json(raw) if raw else None
        except urllib.error.HTTPError as error:
            return error.code, {}, None
        except (OSError, ValueError):
            return 0, {}, None

    def _must(self, node, method, path, body=None, token=None):
        status, headers, value = self._request(node, method, path, body, token)
        if status != 200:
            raise ValueError('CONSUL_API')
        return value

    def _guardian(self):
        left, right = socket.socketpair()
        source = '''import os,socket,signal,sys,time,select,shutil
from pathlib import Path
s=socket.socket(fileno=int(sys.argv[1])); fds=[]; deadline=time.monotonic()+1200
try:
 while time.monotonic()<deadline:
  ready,_,_=select.select([s],[],[],min(1,max(0,deadline-time.monotonic())))
  if not ready: continue
  data=s.recv(64)
  if not data: break
  pid=int(data); fd=os.pidfd_open(pid); fds.append(fd); s.sendall(b"ok")
finally:
 for fd in fds:
  try: signal.pidfd_send_signal(fd,signal.SIGKILL)
  except ProcessLookupError: pass
 pending=list(fds); stopped_by=time.monotonic()+5
 while pending and time.monotonic()<stopped_by:
  ready,_,_=select.select(pending,[],[],max(0,stopped_by-time.monotonic()))
  pending=[fd for fd in pending if fd not in ready]
 for fd in fds: os.close(fd)
 if pending: sys.exit(1)
 for name in OWNED:
  path=RUNTIME/name
  if path.is_symlink(): continue
  if path.is_dir(): shutil.rmtree(path,ignore_errors=True)
  elif path.exists():
   try: path.unlink()
   except OSError: pass
'''
        owned = ['guardian.py', 'readiness.json', 'token-reader']
        for node in ['server'] + self.ids:
            owned.extend(['config-' + node + '.json', 'log-' + node, 'data-' + node])
        for node in self.ids:
            owned.extend(['token-agent-' + node, 'token-writer-' + node])
        source = 'from pathlib import Path\nRUNTIME=Path(' + repr(str(self.runtime)) + ')\nOWNED=' + repr(owned) + '\n' + source
        path = self._file('guardian.py', source)
        self.guard = subprocess.Popen([sys.executable, str(path), str(right.fileno())], pass_fds=(right.fileno(),), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        right.close(); self.guard_socket = left; left.settimeout(3)

    def _spawn(self, node, server=False, agent_token=None):
        # Ports are selected without external listeners. Binding races fail readiness/ownership.
        reservations = []
        ports = {}
        for kind in ('http', 'server', 'serf_lan', 'serf_wan', 'grpc', 'grpc_tls'):
            s = socket.socket(); s.bind(('127.0.0.1', 0)); ports[kind] = s.getsockname()[1]; reservations.append(s)
        self.ports[node] = ports
        name = self.prefix + '-' + node
        self.nodes[node] = name
        data = self.runtime / ('data-' + node); data.mkdir(mode=0o700); self.files.append(data)
        config = dict(node_name=name, datacenter='pilot', data_dir=str(data), bind_addr='127.0.0.1', advertise_addr='127.0.0.1', client_addr='127.0.0.1', server=server, ports=dict(ports, dns=-1), check_update_interval='1s', enable_script_checks=False, enable_local_script_checks=False, log_level='warn', disable_update_check=True, leave_on_terminate=True, acl=dict(enabled=True, default_policy='deny', enable_token_persistence=False, tokens={'agent': agent_token or self.management}))
        if server:
            config.update(bootstrap_expect=1)
            config['acl']['tokens']['initial_management'] = self.management
        else:
            config['retry_join'] = ['127.0.0.1:%d' % self.ports['server']['serf_lan']]
        path = self._file('config-' + node + '.json', json.dumps(config))
        log = self._file('log-' + node, '')
        for s in reservations: s.close()
        with open(log, 'ab') as stream:
            process = subprocess.Popen([self.binary, 'agent', '-config-file=' + str(path)], stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True, umask=0o077)
        self.processes.append(process)
        self.pidfds[node] = os.pidfd_open(process.pid)
        self.guard_socket.sendall(str(process.pid).encode())
        if self.guard_socket.recv(2) != b'ok':
            raise ValueError('WATCHDOG_NOT_ARMED')
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError('CONSUL_START')
            status, _, response = self._request(node, 'GET', '/v1/agent/self', token=agent_token or self.management)
            if status == 200 and isinstance(response, dict) and response.get('Config', {}).get('NodeName') == name:
                self._verify_listener(process.pid, ports['http'])
                return
            time.sleep(.1)
        raise ValueError('CONSUL_READINESS')

    @staticmethod
    def _verify_listener(pid, port):
        inodes = {os.readlink(p) for p in Path('/proc/%d/fd' % pid).iterdir()}
        with open('/proc/net/tcp') as stream:
            for line in stream.readlines()[1:]:
                row = line.split()
                if row[1] == '0100007F:%04X' % port and row[3] == '0A' and 'socket:[%s]' % row[9] in inodes:
                    return
        raise ValueError('ENDPOINT_NOT_OWNED')

    def _token(self, name, rules):
        policy = self._must('server', 'PUT', '/v1/acl/policy', {'Name': self.prefix + '-' + name, 'Rules': rules}, self.management)
        token = self._must('server', 'PUT', '/v1/acl/token', {'Description': 'bounded local pilot', 'Policies': [{'ID': policy['ID']}]}, self.management)
        value = token['SecretID']
        self._file('token-' + name, value)
        return value

    def start(self):
        try:
            self._guardian()
            self._spawn('server', server=True)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status, _, leader = self._request('server', 'GET', '/v1/status/leader', token=self.management)
                if status == 200 and leader: break
                time.sleep(.1)
            else: raise ValueError('CONSUL_LEADER')
            self.reader = self._token('reader', 'node_prefix "" { policy = "read" } service_prefix "" { policy = "read" }')
            for observer in self.ids:
                node = self.prefix + '-' + observer
                # Agent identity can only update its own node; writers cannot mutate others.
                agent = self._token('agent-' + observer, 'node "' + node + '" { policy = "write" } node_prefix "" { policy = "read" } agent "' + node + '" { policy = "write" }')
                self.tokens[observer] = self._token('writer-' + observer, 'node "' + node + '" { policy = "write" }')
                self._spawn(observer, agent_token=agent)
            self._file('readiness.json', json.dumps(dict(http_address='http://127.0.0.1:%d' % self.ports['server']['http'], consul_pid=self.processes[0].pid, read_token_filepath=str(self.runtime / 'token-reader'), owned_pids=[p.pid for p in self.processes])))
            self.metrics['setup_elapsed_ms'] = int((time.monotonic() - self.started_at) * 1000)
            return self
        except BaseException:
            self.stop()
            raise

    def _write_check(self, observer, check_id, report, state):
        if (observer, check_id) not in self.registered:
            self._must(observer, 'PUT', '/v1/agent/check/register', dict(ID=check_id, Name=check_id, TTL=str(self.ttl) + 's', Status='critical'), self.tokens[observer])
            self.registered.add((observer, check_id))
        self._must(observer, 'PUT', '/v1/agent/check/update/' + check_id, dict(Status={'pass': 'passing', 'fail': 'critical', 'unknown': 'warning'}[state], Output=json.dumps(report, separators=(',', ':'))), self.tokens[observer])

    def publish(self, observer, target, check, state, reason, observed_at_ms, seq):
        # Validate even internal callers before putting data into externally visible Output.
        report = dict(observer=observer, target=target, check=check, observed_at_ms=observed_at_ms, seq=seq, state=state, reason=reason)
        sample = dict(schema_version=1, control_plane='ok', topology_kind='real', expected_checks=[check], observers=[dict(id=observer, asn=None, dc=None, physical_domain=None, last_seen_ms=observed_at_ms)], targets=[dict(id=target)], observations=[report])
        evaluate(sample, now_ms=observed_at_ms, max_age_ms=1)
        self._write_check(observer, 'pilot-' + observer + '-' + target + '-' + check, report, state)

    def heartbeat(self, observer, observed_at_ms):
        if observer not in self.ids or type(observed_at_ms) is not int or observed_at_ms < 0:
            raise ValueError('INVALID_HEARTBEAT')
        self._write_check(observer, 'pilot-heartbeat-' + observer, dict(heartbeat=observer, observed_at_ms=observed_at_ms), 'pass')

    def read_consistent(self):
        status, headers, records = self._request('server', 'GET', '/v1/health/state/any?consistent', token=self.reader)
        if status == 403: return 'acl_denied', []
        if status != 200: return 'unavailable', []
        if any(k.lower() == 'x-consul-results-filtered-by-acls' and str(v).lower() == 'true' for k, v in headers.items()):
            return 'partial', []
        if type(records) is not list or any(type(row) is not dict or not all(type(row.get(k)) is str for k in ('Node', 'CheckID', 'Status', 'Output')) or row.get('Status') not in ('passing', 'warning', 'critical') for row in records):
            return 'partial', []
        return 'ok', records

    def snapshot(self, target_ids, expected_checks, topology_kind, observer_metadata):
        cp, records = self.read_consistent()
        reports, heartbeats = [], {}
        try:
            for row in records:
                if row['Node'] not in {self.nodes[x] for x in self.ids} or not row['CheckID'].startswith('pilot-'): continue
                observer = next(x for x in self.ids if self.nodes[x] == row['Node'])
                output = row['Output']
                prefix = 'TTL expired (last output before timeout follows): '
                if output.startswith(prefix):
                    output = output[len(prefix):]
                report = decode_json(output)
                if row['CheckID'] == 'pilot-heartbeat-' + observer:
                    if type(report) is not dict or set(report) != {'heartbeat', 'observed_at_ms'} or report['heartbeat'] != observer or type(report['observed_at_ms']) is not int or report['observed_at_ms'] < 0:
                        raise ValueError('INVALID_HEARTBEAT')
                    heartbeats[observer] = report['observed_at_ms']
                else:
                    if type(report) is not dict or report.get('observer') != observer or row['CheckID'] != 'pilot-' + observer + '-' + str(report.get('target')) + '-' + str(report.get('check')):
                        raise ValueError('INVALID_REPORT')
                    reports.append(report)
            result = dict(schema_version=1, control_plane=cp, topology_kind=topology_kind, expected_checks=list(expected_checks), observers=[dict(id=o, asn=observer_metadata[o]['asn'], dc=observer_metadata[o]['dc'], physical_domain=observer_metadata[o]['physical_domain'], last_seen_ms=heartbeats.get(o)) for o in self.ids], targets=[dict(id=t) for t in target_ids], observations=reports)
            evaluate(result, now_ms=int(time.time() * 1000), max_age_ms=4000)
            return result
        except (ValueError, TypeError, KeyError):
            return dict(schema_version=1, control_plane='partial', topology_kind=topology_kind, expected_checks=list(expected_checks), observers=[dict(id=o, asn=observer_metadata[o]['asn'], dc=observer_metadata[o]['dc'], physical_domain=observer_metadata[o]['physical_domain'], last_seen_ms=None) for o in self.ids], targets=[dict(id=t) for t in target_ids], observations=[])

    def _signal(self, node, signum):
        if node not in self.pidfds:
            raise ValueError('NOT_OWNED')
        signal.pidfd_send_signal(self.pidfds[node], signum)

    def suspend_observer(self, observer):
        if observer not in self.ids: raise ValueError('NOT_OWNED')
        self._signal(observer, signal.SIGSTOP)

    def resume_observer(self, observer):
        if observer not in self.ids: raise ValueError('NOT_OWNED')
        self._signal(observer, signal.SIGCONT)

    def suspend_server(self):
        self._signal('server', signal.SIGSTOP)

    def resume_server(self):
        self._signal('server', signal.SIGCONT)

    def acl_probe(self):
        body = dict(ID='forbidden', Name='forbidden', TTL='5s')
        result = {}
        for name, token in [('anonymous_write', None), ('evaluator_write', self.reader)]:
            code, _, _ = self._request(self.ids[0], 'PUT', '/v1/agent/check/register', body, token)
            result[name] = code
        self.metrics['acl_denials'] = result
        return result

    def sample_resources(self):
        rss, cpu = 0, 0.0
        pids = [p.pid for p in self.processes] + [os.getpid()]
        if self.guard: pids.append(self.guard.pid)
        for pid in pids:
            try:
                values = Path('/proc/%d/stat' % pid).read_text().split()
                cpu += (int(values[13]) + int(values[14])) / os.sysconf('SC_CLK_TCK')
                rss += int(values[23]) * os.sysconf('SC_PAGE_SIZE')
            except (OSError, ValueError, IndexError): pass
        disk = 0
        for path in self.runtime.rglob('*'):
            try:
                if path.is_file(): disk += path.stat().st_size
            except OSError: pass
        sample = dict(rss_bytes=rss, cpu_seconds=cpu, disk_bytes=disk)
        self.resource_samples.append(sample)
        return sample

    def stop(self):
        start = time.monotonic()
        for fd in self.pidfds.values():
            try: signal.pidfd_send_signal(fd, signal.SIGCONT)
            except ProcessLookupError: pass
        for p in self.processes:
            if p.poll() is None: p.terminate()
        for p in self.processes:
            try: p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait(timeout=3)
        if self.guard_socket:
            self.guard_socket.close(); self.guard_socket = None
        if self.guard:
            try: self.guard.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.guard.kill(); self.guard.wait(timeout=3)
        for path in reversed(self.files):
            if path.is_dir(): shutil.rmtree(path)
            elif path.exists(): path.unlink()
        self.files.clear()
        for fd in self.pidfds.values(): os.close(fd)
        self.pidfds.clear()
        self.management = self.reader = None; self.tokens.clear()
        return dict(owned_processes_remaining=sum(p.poll() is None for p in self.processes), runtime_secrets_remaining=sum(1 for _ in self.runtime.iterdir()), elapsed_ms=int((time.monotonic() - start) * 1000))
