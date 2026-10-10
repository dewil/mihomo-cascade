"""Opt-in real dev adapter. Imports are inert; all exported evidence is typed.

No live invocation belongs in unit tests. Credentials travel only through captured
SSH stdin/stdout and private temporary client configuration, never CLI arguments.
"""
import concurrent.futures
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
import resource
from pathlib import Path
import signal
import stat
import socket
import subprocess
import time
import tempfile
from urllib.parse import urlsplit

from dev_fault import DevFault, SSH_BASE

HOST = 'de4.cactushub.app'
NODE = 42
PIN = '9f80affeb492d5e2d8d6c8626107666923e1935ec41c1a3fc6ebcce8846368ec'
SOURCE = Path('/data/git/cactus-adm-geo-as-count/tools/line-probe/line-probe.py')
CORE = '/usr/local/bin/mihomo'
WORKER = 'hiddify-panel-background-tasks.service'
UNITS = ['hiddify-haproxy.service', 'hiddify-nginx.service', WORKER,
         'hiddify-panel.service', 'hiddify-redis.service', 'hiddify-singbox.service',
         'hiddify-ss-faketls.service', 'hiddify-xray.service']
OBSERVERS = ('llm', 'ru', 'ru2')


def now_ms(): return time.time_ns() // 1000000

def typed(state, reason): return {'state': state, 'reason': reason}

def stamped(fn, *args, **kwargs):
    result=fn(*args,**kwargs)
    return result | {'completed_at_ms':now_ms()}

def validate_target(target_host, dev_node_id, allow_dev_faults):
    if (type(target_host) is not str or target_host != HOST or
        type(dev_node_id) is not int or dev_node_id != NODE or
        type(allow_dev_faults) is not bool):
        raise ValueError('DEV_TARGET_NOT_ALLOWED')


def decode_private(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out: raise ValueError('DEV_ADAPTER_INVALID')
            out[key] = value
        return out
    try:
        if len(raw) > 1048576: raise ValueError()
        result = json.loads(raw, object_pairs_hook=pairs)
        if type(result) is not dict or result.get('ok') is not True: raise ValueError()
        return result
    except Exception:
        raise ValueError('DEV_ADAPTER_INVALID') from None


def ssh_json(alias, script, *, php=False, timeout=25):
    if alias not in ('s79-dwl', 'de4-cactus', 'ru-cactus', 'ru2-cactus', 'ru3-cactus'):
        raise ValueError('DEV_SSH_ALIAS')
    argv = SSH_BASE + (['-l', 'root'] if alias == 's79-dwl' else [])
    argv += [alias, '/opt/php85/bin/php' if php else 'python3 -']
    try:
        proc = subprocess.run(argv, input=script.encode(), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout, check=True)
        return decode_private(proc.stdout)
    except Exception:
        raise ValueError('DEV_SSH_FAILED') from None


def php_call(mode):
    if mode not in ('prod', 'profile', 'counter'): raise ValueError('DEV_ADAPTER_MODE')
    return ssh_json('s79-dwl', Path(__file__).with_name('dev_adapters.php').read_text().replace('__MODE__', mode), php=True, timeout=45)


def resolve(host):
    if type(host) is not str or not host or len(host) > 253:
        raise ValueError('DEV_DNS_INVALID')
    try:
        return {str(ipaddress.ip_address(host))}
    except ValueError: pass
    # getaddrinfo may block independently of socket timeouts. Isolate it in a
    # bounded local subprocess, passing public hostnames only through stdin.
    script = 'import socket,json,sys; print(json.dumps(sorted({x[4][0] for x in socket.getaddrinfo(sys.stdin.read(),443,type=socket.SOCK_STREAM)})))'
    try:
        p = subprocess.run(['python3', '-c', script], input=host.encode(), stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=8, check=True)
        result = {str(ipaddress.ip_address(x)) for x in json.loads(p.stdout)}
        if not result: raise ValueError()
        return result
    except Exception:
        raise ValueError('DEV_DNS_UNCERTAIN') from None


def service_state():
    script = r'''import subprocess,json,os,time,re,datetime
units=__UNITS__
def call(args):
 p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=8)
 return p.returncode,p.stdout.decode().strip()
status={u:call(['systemctl','is-active',u])[1]=='active' for u in units}
rc,ss=call(['ss','-Hlnpt','sport = :443'])
liveness={'status':'unknown','age_ms':None}
try:
 with open('/opt/hiddify-manager/log/system/hiddify_panel_background_tasks.err.log','rb') as log:
  before=os.fstat(log.fileno())
  log.seek(0,2); end=log.tell(); start=max(0,end-131072); log.seek(start)
  lines=log.read(131072).decode('utf-8','strict').splitlines()
  after=os.stat('/opt/hiddify-manager/log/system/hiddify_panel_background_tasks.err.log')
  if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino) or after.st_size<end: raise OSError()
  if start: lines=lines[1:]
 for line in reversed(lines[-500:]):
  if not re.search(r'\bTask\s+\S*update_local_usage\S*\s+succeeded\b',line): continue
  match=re.match(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d{1,6})?)',line)
  if not match: break
  stamp=datetime.datetime.fromisoformat(match.group(1).replace(',','.')).timestamp()
  age=int((time.time()-stamp)*1000)
  if age>=0: liveness={'status':'fresh' if age<=150000 else 'stale','age_ms':age}
  break
except (OSError,UnicodeError,ValueError,OverflowError): pass
print(json.dumps({'ok':True,'ssh_server_ip':os.environ.get('SSH_CONNECTION','').split()[-2] if len(os.environ.get('SSH_CONNECTION','').split())==4 else None,'units_active':status,'worker_liveness':liveness,'public443_haproxy':rc==0 and bool(ss) and all('haproxy' in line for line in ss.splitlines())}))
'''.replace('__UNITS__', repr(UNITS))
    return ssh_json('de4-cactus', script)


def preflight():
    prod = php_call('prod')
    hosts = prod.get('prod_hosts')
    if type(hosts) is not list or not hosts or HOST in hosts:
        raise ValueError('DEV_PRODUCTION_OVERLAP')
    dev_ips = resolve(HOST)
    for host in hosts:
        if dev_ips & resolve(host): raise ValueError('DEV_PRODUCTION_OVERLAP')
    profile = php_call('profile')
    if (profile.get('host') != HOST or type(profile.get('node_id')) is not int or
        profile['node_id'] != NODE or profile.get('active') is not True):
        raise ValueError('DEV_PROFILE_INVALID')
    try:
        uri = urlsplit(profile['uri'])
        if uri.scheme != 'vless' or uri.hostname != HOST or uri.port != 443 or not uri.username:
            raise ValueError()
    except Exception: raise ValueError('DEV_PROFILE_INVALID') from None
    state = service_state()
    if (state.get('units_active') != dict.fromkeys(UNITS, True) or
        state.get('public443_haproxy') is not True or state.get('ssh_server_ip') not in dev_ips):
        raise ValueError('DEV_SERVICE_PREFLIGHT')
    profile['verified_ips'] = sorted(dev_ips)
    return profile


def transport(observer):
    script = '''import socket,ssl,time,json,resource
start=time.monotonic()
try:
 with socket.create_connection(('de4.cactushub.app',443),timeout=5) as s:
  with ssl.create_default_context().wrap_socket(s,server_hostname='de4.cactushub.app'): pass
 result={'state':'pass','reason':'ok'}
except TimeoutError: result={'state':'fail','reason':'timeout'}
except OSError: result={'state':'fail','reason':'connect_failed'}
print(json.dumps({'ok':True,'probe':result,'duration_ms':int((time.monotonic()-start)*1000),'resources':{'cpu_seconds':resource.getrusage(resource.RUSAGE_SELF).ru_utime+resource.getrusage(resource.RUSAGE_SELF).ru_stime,'maxrss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}}))
'''
    try:
        if observer == 'llm':
            p = subprocess.run(['python3', '-c', script], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=12, check=True)
            row=decode_private(p.stdout)
            return row['probe'] | {'resources':row['resources']}
        if observer not in ('ru', 'ru2', 'ru3'): raise ValueError()
        row=ssh_json(observer + '-cactus', script, timeout=18)
        return row['probe'] | {'resources':row['resources']}
    except Exception: return typed('unknown', 'probe_error')


def load_client():
    if not SOURCE.is_file() or not Path(CORE).is_file(): raise ValueError('DEV_CLIENT_DEPENDENCY')
    try:
        p = subprocess.run([CORE, '-v'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True)
        if b'1.19.25' not in p.stdout: raise ValueError()
        spec = importlib.util.spec_from_file_location('consul_dev_line_probe', SOURCE)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module
    except Exception: raise ValueError('DEV_CLIENT_DEPENDENCY') from None



def validate_client_paths(directory, config):
    try:
        path=Path(directory); cfg=Path(config)
        ds=path.lstat(); cs=cfg.lstat()
        if (path.parent!=Path('/tmp') or not path.name.startswith('line-probe-core-') or
            not stat.S_ISDIR(ds.st_mode) or stat.S_IMODE(ds.st_mode)!=0o700 or ds.st_uid!=os.getuid() or
            cfg!=path/'config.yaml' or not stat.S_ISREG(cs.st_mode) or stat.S_IMODE(cs.st_mode)!=0o600 or
            cs.st_uid!=os.getuid() or cs.st_nlink!=1): raise ValueError()
        return ds.st_dev,ds.st_ino
    except (OSError,TypeError,ValueError): raise ValueError('DEV_CLIENT_PATH_OWNERSHIP') from None


def controlled_points(points, controls):
    # Internal/configuration failures never become negative target evidence.
    # An unavailable direct control makes a negative endpoint result ambiguous.
    return [typed('unknown','probe_error') if p['state']=='fail' and c['state']!='pass' else p
            for p,c in zip(points,controls)]


class Client:
    def __init__(self, source, uri):
        self.source = source
        self.core = source.CoreProcess({'vless_core_bin':CORE, 'vless_socks_port_range':(34000,44000),
                                        'vless_core_start_timeout_seconds':8}, source.parse_vless_uri(uri))
        self.port = None
        self.watchdog = None
        self.pidfd = None
        self.owned_dir_identity = None
    def __enter__(self):
        mask = os.umask(0o077)
        old_tempdir = tempfile.tempdir
        tempfile.tempdir = '/tmp'
        try:
            self.port = self.core.__enter__()
            self.owned_dir_identity = validate_client_paths(self.core.work_dir,self.core.config_path)
            self.pidfd = os.pidfd_open(self.core.proc.pid)
            # Kernel identity + pipe EOF restore/terminate this owned client even
            # if the caller disappears while the client-path fault is active.
            guard = """import os,sys,signal,select,time,shutil
fd=int(sys.argv[1]); path=sys.argv[2]; identity=(int(sys.argv[3]),int(sys.argv[4]))
print('ARMED',flush=True)
try: select.select([sys.stdin],[],[],3600)
except BaseException: pass
try:
 signal.pidfd_send_signal(fd,signal.SIGCONT)
 signal.pidfd_send_signal(fd,signal.SIGTERM)
 time.sleep(3)
 signal.pidfd_send_signal(fd,signal.SIGKILL)
except ProcessLookupError: pass
if os.path.lexists(path):
 st=os.lstat(path)
 if (st.st_dev,st.st_ino)!=identity or os.path.islink(path): sys.exit(2)
 shutil.rmtree(path)
if os.path.lexists(path): sys.exit(2)
"""
            self.watchdog = subprocess.Popen(['python3','-u','-c',guard,str(self.pidfd),self.core.work_dir,*map(str,self.owned_dir_identity)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                pass_fds=(self.pidfd,),start_new_session=True)
            import selectors
            with selectors.DefaultSelector() as selector:
                selector.register(self.watchdog.stdout,selectors.EVENT_READ)
                if not selector.select(3) or self.watchdog.stdout.readline()!=b'ARMED\n':
                    raise ValueError('DEV_CLIENT_WATCHDOG')
        except Exception:
            self.__exit__(None,None,None)
            raise ValueError('DEV_CLIENT_START') from None
        finally:
            os.umask(mask)
            tempfile.tempdir = old_tempdir
        return self
    def __exit__(self, *exc):
        if self.pidfd is not None:
            try: signal.pidfd_send_signal(self.pidfd,signal.SIGCONT)
            except ProcessLookupError: pass
        try: self.core.__exit__(*exc)
        finally:
            if self.watchdog:
                self.watchdog.stdin.close()
                try: self.watchdog.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    self.watchdog.kill(); self.watchdog.wait(timeout=3)
                self.watchdog.stdout.close()
            if self.pidfd is not None: os.close(self.pidfd); self.pidfd=None
            if self.owned_dir_identity is not None and (os.path.lexists(self.core.work_dir) or
                (self.watchdog is not None and self.watchdog.returncode!=0)):
                raise ValueError('DEV_CLIENT_CLEANUP_FAILED')
    def request(self, endpoint, *, head=False, direct=False, download=False):
        urls = {'primary':'https://www.gstatic.com/generate_204',
                'secondary':'https://speed.cloudflare.com/cdn-cgi/trace',
                'traffic':'https://speed.cloudflare.com/__down?bytes=1048576'}
        if endpoint not in urls: raise ValueError('DEV_ENDPOINT')
        argv = ['curl','--silent','--show-error','--max-time','25' if download else '5',
                '--connect-timeout','5','--max-filesize','1048576','--proto','=https',
                '--write-out','\n%{http_code}']
        if not direct: argv += ['--socks5-hostname','127.0.0.1:%d' % self.port]
        else: argv += ['--noproxy','*']
        if head: argv += ['--head']
        argv += [urls[endpoint]]
        try:
            p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=28 if download else 7)
            if p.returncode in (7,28): return typed('fail','connect_failed' if p.returncode==7 else 'timeout')
            if p.returncode: return typed('unknown','probe_error')
            body, status = p.stdout.rsplit(b'\n',1); code=int(status)
            good = (code > 0 if head else
                    (code == 204 and not body) if endpoint == 'primary' else
                    (code == 200 and len(body)==1048576) if download else
                    (code == 200 and b'ip=' in body))
            return typed('pass','ok') if good else typed('fail','http_status' if code != 200 else 'body_mismatch')
        except Exception: return typed('unknown','probe_error')


class Accounting:
    def __init__(self): self.previous=None; self.last_advance=None; self.first=None
    def sample(self):
        try:
            row=php_call('counter'); value=row['counter_bytes']; stamp=now_ms()
            if type(value) not in (int,float) or not math.isfinite(value) or value<0: raise ValueError()
            if self.first is None: self.first=stamp
            delta=0 if self.previous is None else value-self.previous
            if delta<0: raise ValueError()
            self.previous=value
            if delta>0: self.last_advance=stamp
            age=stamp-(self.last_advance if self.last_advance is not None else self.first)
            result=typed('pass','ok') if self.last_advance is not None and age<=150000 else (
                typed('fail','accounting_stale') if age>150000 else typed('unknown','missing'))
            return result | {'counter_advanced':delta>0,'delta_bytes':round(delta),'sample_age_ms':age}
        except Exception: return typed('unknown','probe_error') | {'counter_advanced':False,'delta_bytes':None,'sample_age_ms':None}


def accounting_window(client, accounting):
    before=accounting.sample()
    if before['reason']=='probe_error': return False
    if client.request('traffic',download=True)['state']!='pass': return False
    deadline=time.monotonic()+150
    while time.monotonic()<deadline:
        sample=accounting.sample()
        if sample['counter_advanced']: return sample
        time.sleep(5)
    return False



def process_resources(pid):
    try:
        stat=Path('/proc/%d/stat'%pid).read_text().rsplit(')',1)[1].split()
        status=Path('/proc/%d/status'%pid).read_text().splitlines()
        rss=next(int(x.split()[1])*1024 for x in status if x.startswith('VmRSS:'))
        return {'cpu_seconds':(int(stat[11])+int(stat[12]))/os.sysconf('SC_CLK_TCK'),'rss_bytes':rss}
    except (OSError,ValueError,StopIteration):
        return {'cpu_seconds':None,'rss_bytes':None,'reason':'process_exited_or_unreadable'}


def run_dev(*, consul_binary, runtime_dir, output_dir, target_host, dev_node_id, allow_dev_faults=False):
    validate_target(target_host,dev_node_id,allow_dev_faults)  # Must precede every IO.
    source_names=('consul_pilot.py','consul_backend.py','lab_runner.py','dev_runner.py','dev_fault.py','dev_adapters.php')
    source_hashes={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in source_names}
    from consul_backend import ConsulBackend
    from consul_pilot import evaluate, combine_application, export_events
    binary=Path(consul_binary); runtime=Path(runtime_dir); output=Path(output_dir)
    if (not binary.is_absolute() or not binary.is_file() or
        hashlib.sha256(binary.read_bytes()).hexdigest()!=PIN): raise ValueError('CONSUL_DEPENDENCY')
    if not runtime.is_absolute() or runtime.is_symlink() or (runtime.exists() and any(runtime.iterdir())):
        raise ValueError('RUNTIME_NOT_EMPTY')
    # Resolve parents to exclude symlink aliases of synced directories.
    if any(part in ('sync','obs','GoogleDrive') for part in runtime.resolve().parts):
        raise ValueError('RUNTIME_SYNCED')
    if not output.is_absolute() or output.resolve()==runtime.resolve() or runtime.resolve() in output.resolve().parents:
        raise ValueError('OUTPUT_INVALID')
    runtime.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(runtime,0o700)
    output.mkdir(mode=0o700,parents=True,exist_ok=True)
    for name in ('report.md','evidence.json'):
        if (output/name).exists() or (output/name).is_symlink(): raise ValueError('OUTPUT_EXISTS')
    source=load_client()
    backend=ConsulBackend(str(binary),str(runtime),list(OBSERVERS),ttl_seconds=10)
    scenarios=[]; samples=[]; seq=0; healthy=False; error=None
    cleanup={'owned_processes_remaining':0,'runtime_secrets_remaining':0}
    metadata={o:{'asn':None,'dc':None,'physical_domain':None} for o in OBSERVERS}
    account=Accounting()
    current_fault=None
    owned_resource_samples=[]
    cycle_starts=[]
    child_usage_before=resource.getrusage(resource.RUSAGE_CHILDREN)
    started=now_ms()

    def collect(client, *, lost=False, cp_down=False):
        nonlocal seq
        seq+=1
        backend.sample_resources()
        tick=now_ms()
        cycle_starts.append(tick)
        owned_resource_samples.append({'at_ms':tick,'client':process_resources(client.core.proc.pid),
                                       'client_watchdog':process_resources(client.watchdog.pid),
                                       'client_disk_bytes':sum(p.stat().st_size for p in Path(client.core.work_dir).rglob('*') if p.is_file())})
        with concurrent.futures.ThreadPoolExecutor(max_workers=11) as pool:
            futures={o:pool.submit(stamped,transport,o) for o in OBSERVERS}
            primary=pool.submit(stamped,client.request,'primary')
            secondary=pool.submit(stamped,client.request,'secondary')
            direct_a=pool.submit(client.request,'primary',direct=True)
            direct_b=pool.submit(client.request,'secondary',direct=True)
            comparator=pool.submit(client.request,'primary',head=True)
            counter=pool.submit(stamped,account.sample)
            services=pool.submit(service_state)
            trans={o:f.result() for o,f in futures.items()}
            points=[primary.result(),secondary.result()]
            controls=[direct_a.result(),direct_b.result()]
            baseline=comparator.result(); accounting=counter.result(); service=services.result()
            service.pop('ssh_server_ip',None)
        points=controlled_points(points,controls)
        application=combine_application(*[{k:p[k] for k in ('state','reason')} for p in points])
        application['completed_at_ms']=min(primary.result()['completed_at_ms'],secondary.result()['completed_at_ms'])
        measured=now_ms()
        for observer in OBSERVERS:
            if lost and observer=='llm': continue
            if not cp_down:
                backend.heartbeat(observer,measured)
                for check,result in [('transport',trans[observer]),('application',application if observer=='llm' else typed('unknown','missing')),
                                     ('accounting',accounting if observer=='llm' else typed('unknown','missing'))]:
                    backend.publish(observer,'de4',check,result['state'],result['reason'],result.get('completed_at_ms',measured),seq)
        snapshot=backend.snapshot(['de4'],['transport','application','accounting'],'real',metadata)
        assessed=evaluate(snapshot,now_ms=now_ms(),max_age_ms=15000)
        for cell in assessed['cells']:
            samples.append({'subject':'consul-pilot/%s/%s/%s'%(cell['observer'],cell['target'],cell['check']),
                            'measured_at_ms':assessed['evaluated_at_ms'],'state':{'pass':'ok','fail':'failing','unknown':'unknown'}[cell['state']],
                            'reason':cell['reason'],'evidence_ids':['sample-%d'%seq]})
        return {'at_ms':measured,'started_at_ms':tick,'actual_cycle_period_ms':None if len(cycle_starts)<2 else tick-cycle_starts[-2],'cycle_duration_ms':now_ms()-tick,'transport':trans,'application':application,
                'accounting':accounting,'local_services':service,'baseline_comparator':baseline,'control_points':controls,
                'application_points':points,'evaluation':assessed,
                'false_path_down':int(baseline['state']!='pass' and application['state']=='pass')}

    def row(name):
        return {'id':name,'repeat':1,'proof_kind':'real_dev','fault_mechanism':name,
                'injected_at_ms':None,'removed_at_ms':None,'detected_at_ms':None,'recovered_at_ms':None,
                'false_path_down':0,'unknown_duration_ms':0,'rollback':{},'samples':[],'passed':False}

    def add(r,s):
        r['samples'].append(s); r['false_path_down']+=s['false_path_down']
        if any(c['state']=='unknown' for c in s['evaluation']['cells']):
            r['unknown_duration_ms']+=s['at_ms']-s['started_at_ms']

    def recovered(client):
        state=service_state()
        if state.get('units_active')!=dict.fromkeys(UNITS,True) or state.get('public443_haproxy') is not True:
            return False
        advanced=accounting_window(client,account)
        if not advanced: return False
        fresh=collect(client)
        fresh['accounting_recovery']=advanced
        return fresh if fresh['application']['state']=='pass' else False

    try:
        profile=preflight()
        backend.start()
        backend.acl_probe()
        with Client(source,profile.pop('uri')) as client:
            baseline=row('healthy'); scenarios.append(baseline)
            baseline['baseline_at_ms']=now_ms()
            advanced=accounting_window(client,account)
            baseline['accounting_baseline']=advanced if advanced else None
            fresh=collect(client); add(baseline,fresh)
            healthy=bool(advanced) and fresh['application']['state']=='pass' and all(v['state']=='pass' for v in fresh['transport'].values())
            baseline['passed']=healthy
            if not healthy: raise ValueError('DEV_BASELINE_FAILED')
            if allow_dev_faults:
                for name in ('service_down','worker_hang','client_path_bad','observer_lost','control_plane_unavailable'):
                    # Every cycle refreshes ownership/IP/units and actual accounting.
                    check=preflight(); check.pop('uri',None)
                    advanced=accounting_window(client,account)
                    if not advanced: raise ValueError('DEV_ACCOUNTING_BASELINE')
                    r=row(name); scenarios.append(r)
                    r['accounting_baseline']=advanced
                    r['baseline_at_ms']=now_ms()
                    pidfd=None; suspended=False
                    try:
                        if name in ('service_down','worker_hang'):
                            current_fault=DevFault(name,UNITS,WORKER,check['verified_ips'])
                            current_fault.start(); r['injected_at_ms']=current_fault.injected_at_ms
                            r['rollback']['watchdog_armed_before_fault']=current_fault.armed
                            # Controlled real traffic during worker suspension.
                            if name=='worker_hang':
                                if client.request('traffic',download=True)['state']!='pass': raise ValueError('DEV_WORKER_TRAFFIC')
                        elif name=='client_path_bad':
                            pidfd=os.pidfd_open(client.core.proc.pid)
                            signal.pidfd_send_signal(pidfd,signal.SIGSTOP); suspended=True
                            r['injected_at_ms']=now_ms()
                        elif name=='observer_lost':
                            backend.suspend_observer('llm'); suspended=True; r['injected_at_ms']=now_ms()
                        else:
                            backend.suspend_server(); suspended=True; r['injected_at_ms']=now_ms()
                        duration=170 if name=='worker_hang' else 15
                        deadline=time.monotonic()+duration
                        while time.monotonic()<deadline:
                            tick=time.monotonic()
                            s=collect(client,lost=name=='observer_lost',cp_down=name=='control_plane_unavailable'); add(r,s)
                            if name in ('service_down','client_path_bad'):
                                detected=s['application']['state']=='fail'
                            elif name=='worker_hang':
                                detected=(s['accounting']['reason']=='accounting_stale' and s['application']['state']=='pass'
                                          and s['local_services']['worker_liveness']['status']=='stale')
                            else:
                                cells=[c for c in s['evaluation']['cells'] if name!='observer_lost' or c['observer']=='llm']
                                detected=bool(cells) and all(c['state']=='unknown' for c in cells)
                            if detected and r['detected_at_ms'] is None: r['detected_at_ms']=s['at_ms']
                            time.sleep(max(0,5-(time.monotonic()-tick)))
                    finally:
                        if current_fault:
                            restored=current_fault.wait_removed(); r['removed_at_ms']=restored['at_ms']
                            r['rollback']['remote_units_active']=restored['units_active']
                            r['rollback']['remote_resources']=restored['resources']
                            current_fault.close(); current_fault=None
                        elif name=='client_path_bad' and pidfd is not None:
                            signal.pidfd_send_signal(pidfd,signal.SIGCONT); os.close(pidfd); r['removed_at_ms']=now_ms()
                        elif suspended:
                            if name=='observer_lost': backend.resume_observer('llm')
                            else: backend.resume_server()
                            r['removed_at_ms']=now_ms()
                    recovery=recovered(client)
                    r['rollback']['new_application_and_counter_verified']=bool(recovery)
                    if recovery:
                        add(r,recovery); r['recovered_at_ms']=recovery['at_ms']
                    r['passed']=r['detected_at_ms'] is not None and bool(recovery)
                    if not r['passed']: raise ValueError('DEV_SCENARIO_FAILED')
    except Exception as exc:
        if not backend.processes:
            raise ValueError('DEV_PREFLIGHT_FAILED') from None
        allowed={'DEV_BASELINE_FAILED','DEV_ACCOUNTING_BASELINE','DEV_SCENARIO_FAILED','DEV_WORKER_TRAFFIC'}
        error=str(exc) if isinstance(exc,ValueError) and str(exc) in allowed else 'DEV_RUN_FAILED'
    finally:
        if current_fault: current_fault.close()
        cleanup=backend.stop()
    child_usage_after=resource.getrusage(resource.RUSAGE_CHILDREN)
    acceptance=bool(allow_dev_faults and not error and healthy and len(scenarios)==6 and all(r['passed'] for r in scenarios))
    evidence={'schema_version':1,'mode':'dev','source_hashes':source_hashes,'consul':backend.metrics,'scenarios':scenarios,
              'events':export_events(samples),'resources':{'elapsed_ms':now_ms()-started,'consul_samples':backend.resource_samples,'client_and_watchdog_samples':owned_resource_samples,
                'local_reaped_children_cpu_seconds':child_usage_after.ru_utime+child_usage_after.ru_stime-child_usage_before.ru_utime-child_usage_before.ru_stime,
                'largest_local_child_peak_rss_kib_process_lifetime':child_usage_after.ru_maxrss,
                'method':'Linux proc stat/status sampled client/watchdog and Consul; getrusage reaped local children includes ephemeral curl/SSH/DNS, not remote workloads; child maxrss is largest single child, not total concurrent RSS.',
                'unavailable':['Remote PHP process RSS/CPU not measured.', 'Concurrent total RSS including ephemeral local/remote subprocesses is unavailable.', 'Remote observer/watchdog disk writes not measured; adapters do not create files.'],
                'remote_observers':'self CPU/maxrss in transport results; remote fault/watchdog CPU/maxrss in rollback resources'},'cleanup':cleanup,
              'recommendation':'limit' if acceptance else 'reject',
              'limitations':['Single repeat per live scenario; no statistical confidence.',
                             'Observer ASN/DC/physical domains unknown; no independent quorum claim.',
                             'Counter delta includes overhead and possible concurrent probe traffic.',
                             'Both comparators share collection cycles with requested minimum period 5s; actual_cycle_period_ms and cycle_duration_ms record overruns. Production HEAD cadence is 30s.',
                             'Freshness accounting 150s; worker schedule 60s. Bounded worker log is liveness only; worker detection requires stale liveness AND frozen counter AND live application.',
                             'Real dev evidence does not establish geographic outage, production HA or user-line behavior.'],
              'requirement_evidence':{**{'REQ-CONSUL-%02d'%n:('dev scenarios' if n in (4,5,6,7,8) else 'see laboratory report') for n in range(1,10)},
                                      **{'INV-CONSUL-%02d'%n:'typed real Consul snapshots; evaluator tests' for n in range(1,4)}},
              'acceptance_complete':acceptance,'error':error,
              'sources':{'line_probe_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                         'mihomo_version':'1.19.25','comparator_method':'HEAD','comparator_redirects':False,
                         'comparator_expected_status':'*','production_interval_seconds':30,'experimental_interval_seconds':5}}
    (output/'evidence.json').write_text(json.dumps(evidence,indent=2)+'\n')
    report=('Dev Consul pilot\n\nAcceptance complete: %s. Outcome: %s.\n\n'%(acceptance,'failed' if error else 'baseline only' if not allow_dev_faults else 'passed')+
            '\n'.join('- '+x for x in evidence['limitations'])+'\n\nScenario results:\n'+
            '\n'.join('- %s: %s; baseline counter delta=%s bytes; recovery counter delta=%s bytes'%(r['id'],'passed' if r['passed'] else 'failed',
                (r.get('accounting_baseline') or {}).get('delta_bytes'),
                next((sample['accounting_recovery']['delta_bytes'] for sample in r['samples'] if 'accounting_recovery' in sample),None)) for r in scenarios)+
            '\n\nSource hashes captured before execution:\n'+json.dumps(source_hashes,sort_keys=True)+'\n\nProduction delivery is disabled. Full requirement mapping and typed samples are in evidence.json.\n')
    (output/'report.md').write_text(report)
    return {'schema_version':1,'mode':'dev','outcome':'failed' if error else 'passed',
            'acceptance_complete':acceptance,'report':str(output/'report.md'),'evidence':str(output/'evidence.json'),
            'consul_version':'2.0.4','cleanup':cleanup}
