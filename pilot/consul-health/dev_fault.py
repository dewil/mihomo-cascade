"""Fixed dev-only remote fault program; no caller-provided shell fragments.

The forked watchdog owns kernel pidfds, acknowledges readiness before STOP,
ignores SIGHUP and restores independently of the SSH connection. This module
contains no operation at import time. Remote execution requires reviewed opt-in.
"""
import json
import ipaddress
import selectors
import subprocess
import time

SSH_BASE = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=5',
            '-o', 'ServerAliveCountMax=2']

# Executed only on the fixed dev SSH alias. Identifiers below are constants.
REMOTE = r'''
import os, signal, subprocess, sys, time, json, select, resource
KIND = __KIND__
UNITS = __UNITS__
WORKER = __WORKER__
VERIFIED_IPS = __VERIFIED_IPS__
def run(*args):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=8, check=True).stdout.decode().strip()
def active():
    return all(run('systemctl', 'is-active', u) == 'active' for u in UNITS)
def identity(pid):
    return open('/proc/%d/stat' % pid).read().rsplit(')', 1)[1].split()[19]
def worker_state(fd):
    # Read the PID associated with this retained kernel handle, and check its
    # liveness again after /proc access so PID reuse cannot verify another task.
    signal.pidfd_send_signal(fd,0)
    fields=dict(line.split(':',1) for line in open('/proc/self/fdinfo/%d'%fd) if ':' in line)
    pid=int(fields['Pid'].strip())
    if pid<=0: raise ProcessLookupError()
    state=open('/proc/%d/stat'%pid).read().rsplit(')',1)[1].split()[0]
    signal.pidfd_send_signal(fd,0)
    return state

def resume_workers(fds):
    failed=False
    for fd in fds:
        try: signal.pidfd_send_signal(fd,signal.SIGCONT)
        except ProcessLookupError: pass
        except OSError: failed=True
    deadline=time.monotonic()+2
    while True:
        stopped=False
        for fd in fds:
            try: stopped=worker_state(fd) in ('T','t') or stopped
            except ProcessLookupError: pass
            except (OSError,ValueError,KeyError): failed=True
        if not stopped or time.monotonic()>=deadline: return not (failed or stopped)
        time.sleep(0.05)
fds=[]
try:
    connection=os.environ.get('SSH_CONNECTION','').split()
    if len(connection)!=4 or connection[2] not in VERIFIED_IPS: raise RuntimeError()
    if not active(): raise RuntimeError()
    public = run('ss', '-Hlnpt', 'sport = :443')
    if not public or any('haproxy' not in x for x in public.splitlines()): raise RuntimeError()
    if KIND == 'worker_hang':
        root = int(run('systemctl', 'show', WORKER, '-p', 'MainPID', '--value'))
        if root <= 1: raise RuntimeError()
        group = run('systemctl', 'show', WORKER, '-p', 'ControlGroup', '--value')
        if group != '/system.slice/hiddify-panel-background-tasks.service': raise RuntimeError()
        from pathlib import Path
        cg=Path('/sys/fs/cgroup') / group.lstrip('/')
        pids=set()
        for p in cg.rglob('cgroup.procs'): pids.update(int(x) for x in p.read_text().split())
        if root not in pids or not pids: raise RuntimeError()
        for pid in sorted(pids):
            before=identity(pid); fd=os.pidfd_open(pid)
            if identity(pid) != before: raise RuntimeError()
            signal.pidfd_send_signal(fd, 0)
            fds.append(fd)
    timeout = 45 if KIND == 'service_down' else 195
    duration = 30 if KIND == 'service_down' else 180
    ready_r,ready_w=os.pipe(); done_r,done_w=os.pipe(); restored_r,restored_w=os.pipe()
    child=os.fork()
    if child == 0:
        os.close(ready_r); os.close(done_w); os.close(restored_r)
        os.setsid(); signal.signal(signal.SIGHUP, signal.SIG_IGN)
        null=os.open('/dev/null',os.O_RDWR)
        for fd in (0,1,2): os.dup2(null,fd)
        os.write(ready_w,b'ARMED'); os.close(ready_w)
        restored=False; restored_at=None
        try:
            try:
                # Reserve two bounded systemctl calls for start + verification.
                select.select([done_r],[],[],timeout-(16 if KIND=='service_down' else 10))
            finally:
                if KIND == 'service_down':
                    run('systemctl','start','hiddify-haproxy.service')
                    restored_at=int(time.time()*1000)
                    if run('systemctl','is-active','hiddify-haproxy.service')!='active': raise RuntimeError()
                else:
                    if not resume_workers(fds): raise RuntimeError()
                    restored_at=int(time.time()*1000)
                    if run('systemctl','is-active',WORKER)!='active': raise RuntimeError()
                restored=True
        except BaseException: pass
        finally:
            try: os.write(restored_w,json.dumps({'restored':restored,'at_ms':restored_at}).encode())
            except OSError: pass
            os.close(restored_w)
            os._exit(0 if restored else 2)
    os.close(ready_w); os.close(done_r); os.close(restored_w)
    if not select.select([ready_r],[],[],3)[0] or os.read(ready_r,5)!=b'ARMED': raise RuntimeError()
    os.close(ready_r)
    # ARM acknowledgement is emitted before the mutating operation.
    print(json.dumps({'phase':'armed','watchdog_independent':True}),flush=True)
    if KIND == 'service_down': run('systemctl','stop','hiddify-haproxy.service')
    else:
        for fd in fds: signal.pidfd_send_signal(fd,signal.SIGSTOP)
    print(json.dumps({'phase':'injected','at_ms':int(time.time()*1000)}),flush=True)
    time.sleep(duration-8)
    os.write(done_w,b'RESTORE'); os.close(done_w)
    _,status=os.waitpid(child,0)
    restored=json.loads(os.read(restored_r,2048)); os.close(restored_r)
    if status!=0 or restored.get('restored') is not True or type(restored.get('at_ms')) is not int: raise RuntimeError()
    if not active(): raise RuntimeError()
    print(json.dumps({'phase':'removed','at_ms':restored['at_ms'],'watchdog_restored':True,'units_active':True,'resources':{'self_cpu_seconds':resource.getrusage(resource.RUSAGE_SELF).ru_utime+resource.getrusage(resource.RUSAGE_SELF).ru_stime,'children_cpu_seconds':resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime+resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime,'largest_child_maxrss_kib':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,'method':'getrusage reaped watchdog/systemctl children; not total concurrent RSS'}}),flush=True)
except BaseException:
    # Closing the parent pipe also wakes the independent recovery process.
    print(json.dumps({'phase':'error','code':'DEV_FAULT_FAILED'}),flush=True)
    sys.exit(2)
'''


class DevFault:
    """Bounded remote child. Call wait_removed before authorizing another fault."""
    def __init__(self, kind, units, worker, verified_ips):
        if (kind not in ('service_down', 'worker_hang') or
            worker != 'hiddify-panel-background-tasks.service' or
            set(units) != {'hiddify-haproxy.service','hiddify-nginx.service',
                           'hiddify-panel-background-tasks.service','hiddify-panel.service',
                           'hiddify-redis.service','hiddify-singbox.service',
                           'hiddify-ss-faketls.service','hiddify-xray.service'} or len(units)!=8):

            raise ValueError('DEV_FAULT_KIND')
        try:
            if not verified_ips or len(verified_ips)>16: raise ValueError()
            self.verified_ips = [str(ipaddress.ip_address(x)) for x in verified_ips]
        except Exception: raise ValueError('DEV_FAULT_IP_GUARD') from None
        self.kind, self.units, self.worker = kind, units, worker
        self.proc = None
        self.armed = False
        self.injected_at_ms = None
        self.removed_at_ms = None

    def start(self):
        script = REMOTE.replace('__KIND__', repr(self.kind)).replace('__UNITS__', repr(self.units)).replace('__WORKER__', repr(self.worker)).replace('__VERIFIED_IPS__',repr(self.verified_ips))
        self.proc = subprocess.Popen(SSH_BASE + ['de4-cactus', 'python3 -u -'],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        self.proc.stdin.write(script.encode()); self.proc.stdin.close()
        first = self._line(20)
        if first != {'phase': 'armed', 'watchdog_independent': True}:
            raise ValueError('DEV_WATCHDOG_NOT_ARMED')
        self.armed = True
        second = self._line(15)
        if second.get('phase') != 'injected' or type(second.get('at_ms')) is not int:
            raise ValueError('DEV_FAULT_NOT_CONFIRMED')
        self.injected_at_ms = second['at_ms']
        return self

    def _line(self, timeout):
        deadline = time.monotonic() + timeout
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline and len(data) < 2048:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                # Unbuffered single bytes avoid TextIO buffering hiding later lines.
                import os
                byte = os.read(self.proc.stdout.fileno(), 1)
                if not byte: break
                if byte == b'\n':
                    try: return json.loads(data)
                    except Exception: break
                data.extend(byte)
        raise ValueError('DEV_FAULT_CHANNEL_FAILED')

    def wait_removed(self):
        row = self._line(205 if self.kind == 'worker_hang' else 55)
        if (row.get('phase') != 'removed' or row.get('units_active') is not True or
            row.get('watchdog_restored') is not True or type(row.get('at_ms')) is not int):
            raise ValueError('DEV_ROLLBACK_UNVERIFIED')
        self.removed_at_ms = row['at_ms']
        return row

    def close(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try: self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill(); self.proc.wait(timeout=5)
            self.proc.stdout.close()
