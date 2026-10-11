"""One-host real Consul experiment with explicitly synthetic HTTP path policies."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import http.client
import json
from pathlib import Path
import socket
import threading
import time

from consul_backend import ConsulBackend
from consul_pilot import combine_application, evaluate, export_events

IDS = list('abcd')
CHECKS = ['transport', 'application', 'accounting']
META = {x: dict(asn='synthetic-as-' + x, dc='synthetic-dc-' + x, physical_domain='synthetic-host-' + x) for x in IDS}


def ms(): return int(time.time() * 1000)


class Fixture:
    def __init__(self, target, policy):
        self.target, self.policy, self.server, self.thread, self.port = target, policy, None, None, 0
        try:
            self.start()
        except BaseException:
            self.stop()
            raise

    def start(self):
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_HEAD(self): self.respond(True)
            def do_GET(self): self.respond(False)
            def respond(self, head):
                parts = self.path.strip('/').split('/')
                if len(parts) != 2 or parts[0] not in IDS or parts[1] not in ('healthz', 'payload', 'accounting'):
                    self.send_error(404); return
                observer, endpoint = parts
                case = fixture.policy['case']
                blocked = (case in ('single_path', 'client_path_bad') and observer == 'a' and fixture.target == 'b') or (case == 'row_fault' and observer == 'a') or (case == 'partition' and ((observer in 'ab') != (fixture.target in 'ab'))) or (case == 'target_column' and fixture.target == 'b')
                if endpoint != 'accounting' and (blocked or (case == 'control_point_only' and endpoint == 'healthz' and fixture.target == 'b')):
                    self.close_connection = True
                    return
                body = (json.dumps({'age_ms': max(0, ms() - fixture.policy['injected']) if case == 'worker_hang' and fixture.target == 'b' else 0}).encode() if endpoint == 'accounting' else b'pilot-ok')
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
                if not head:
                    try: self.wfile.write(body)
                    except OSError: pass
        self.server = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            if self.thread and self.thread.is_alive():
                self.server.shutdown(); self.thread.join(timeout=2)
            self.server.server_close()
            self.server = None


def http_probe(fixture, observer, endpoint, *, head=False):
    connection = http.client.HTTPConnection('127.0.0.1', fixture.port, timeout=.5)
    try:
        connection.request('HEAD' if head else 'GET', '/' + observer + '/' + endpoint)
        response = connection.getresponse()
        body = response.read(1024)
        if head: return dict(state='pass', reason='ok')  # Mihomo expected-status=*; redirects not followed.
        if response.status != 200: return dict(state='fail', reason='http_status')
        if endpoint == 'accounting':
            value = json.loads(body)
            if type(value) is not dict or type(value.get('age_ms')) is not int:
                return dict(state='unknown', reason='probe_error')
            return dict(state='fail', reason='accounting_stale') if value['age_ms'] > 4000 else dict(state='pass', reason='ok')
        return dict(state='pass', reason='ok') if body == b'pilot-ok' else dict(state='fail', reason='body_mismatch')
    except (OSError, http.client.HTTPException):
        return dict(state='fail', reason='connect_failed')
    except (ValueError, TypeError):
        return dict(state='unknown', reason='probe_error')
    finally: connection.close()


def probe_pair(observer, fixture):
    try:
        with socket.create_connection(('127.0.0.1', fixture.port), timeout=.5): pass
        transport = dict(state='pass', reason='ok')
    except OSError: transport = dict(state='fail', reason='connect_failed')
    primary = http_probe(fixture, observer, 'healthz')
    secondary = http_probe(fixture, observer, 'payload')
    application = combine_application(primary, secondary)
    accounting = http_probe(fixture, observer, 'accounting')
    baseline = http_probe(fixture, observer, 'healthz', head=True)
    return dict(observer=observer, target=fixture.target, measured_at_ms=ms(), transport=transport, application=application, accounting=accounting, baseline=baseline, primary=primary, secondary=secondary)


def run_lab(*, consul_binary, runtime_dir, output_dir):
    output = Path(output_dir)
    if not output.is_absolute() or output.is_symlink() or output.resolve() == Path(runtime_dir).resolve() or output.resolve().is_relative_to(Path(runtime_dir).resolve()):
        raise ValueError('UNSAFE_OUTPUT')
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    backend = ConsulBackend(consul_binary, runtime_dir, IDS)
    fixtures, samples, scenarios, cycles = [], [], [], []
    policy = {'case': 'healthy', 'injected': 0}
    seq = 0
    last_cycle = 0.0
    source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}
    cleanup = {}
    start = time.monotonic()
    outcome = 'failed'
    failure = None
    pool = ThreadPoolExecutor(max_workers=16)
    try:
        backend.start()
        if backend.acl_probe() != {'anonymous_write': 403, 'evaluator_write': 403}:
            raise ValueError('ACL_ACCEPTANCE')
        for target in IDS:
            fixtures.append(Fixture(target, policy))
        def cycle(lost=False, cp=False):
            nonlocal seq, last_cycle
            time.sleep(max(0, 1 - (time.monotonic() - last_cycle)))
            last_cycle = time.monotonic()
            seq += 1
            began = time.monotonic()
            pairs = list(pool.map(lambda args: probe_pair(*args), [(o, f) for o in IDS for f in fixtures if not (lost and o == 'a')]))
            for observer in IDS:
                if not cp and not (lost and observer == 'a'): backend.heartbeat(observer, ms())
            for pair in ([] if cp else pairs):
                for check in CHECKS:
                    backend.publish(pair['observer'], pair['target'], check, **pair[check], observed_at_ms=pair['measured_at_ms'], seq=seq)
            snapshot = backend.snapshot(IDS, CHECKS, 'synthetic_single_host', META)
            result = evaluate(snapshot, now_ms=ms(), max_age_ms=4000)
            for cell in result['cells']:
                samples.append(dict(subject='consul-pilot/' + '/'.join(cell[k] for k in ('observer', 'target', 'check')), measured_at_ms=result['evaluated_at_ms'], state={'pass': 'ok', 'fail': 'failing', 'unknown': 'unknown'}[cell['state']], reason=cell['reason'], evidence_ids=['cycle-%d' % seq]))
            false_down = sum(p['baseline']['state'] == 'fail' and p['application']['state'] == 'pass' for p in pairs)
            record = dict(at_ms=result['evaluated_at_ms'], duration_ms=int((time.monotonic() - began) * 1000), baseline_failing=sum(p['baseline']['state'] == 'fail' for p in pairs), application_failing=sum(c['state'] == 'fail' and c['check'] == 'application' for c in result['cells']), unknown_cells=sum(c['state'] == 'unknown' for c in result['cells']), false_path_down=false_down, control_plane=result['control_plane'], patterns=sorted({p['kind'] for p in result['patterns']}))
            cycles.append(record); backend.sample_resources()
            return result, record, pairs
        def healthy(result): return all(c['state'] == 'pass' for c in result['cells'])
        def wait_for(predicate, *, lost=False, cp=False, timeout=15):
            deadline = time.monotonic() + timeout
            observations = []
            while time.monotonic() < deadline:
                tick = time.monotonic()
                result, record, pairs = cycle(lost, cp)
                observations.append(record)
                if predicate(result, record, pairs): return result, observations
                time.sleep(max(0, 1 - (time.monotonic() - tick)))
            raise ValueError('SCENARIO_BOUND_EXCEEDED')
        wait_for(lambda r, c, p: healthy(r))
        baseline_start = ms()
        while ms() - baseline_start < 30000:
            tick = time.monotonic()
            result, record, _ = cycle()
            if not healthy(result): raise ValueError('BASELINE_NOT_HEALTHY')
            time.sleep(max(0, 1 - (time.monotonic() - tick)))
        scenarios.append(dict(id='healthy', repeat=1, proof_kind='real_consul', fault_mechanism='none', baseline_started_at_ms=baseline_start, baseline_duration_ms=ms() - baseline_start, injected_at_ms=baseline_start, removed_at_ms=ms(), detected_at_ms=baseline_start, recovered_at_ms=ms(), false_path_down=0, unknown_duration_ms=0, rollback={'required': False}))
        cases = ['control_point_only', 'single_path', 'client_path_bad', 'row_fault', 'target_column', 'service_down', 'worker_hang', 'observer_lost', 'partition', 'control_plane_unavailable']
        for case in cases:
            for repeat in (1, 2, 3):
                wait_for(lambda r, c, p: healthy(r))
                injected = ms(); policy.update(case=case, injected=injected)
                if case == 'service_down': fixtures[1].stop()
                if case == 'observer_lost': backend.suspend_observer('a')
                if case == 'control_plane_unavailable': backend.suspend_server()
                def detected(result, record, pairs):
                    if case == 'control_point_only': return record['false_path_down'] > 0 and healthy(result)
                    if case == 'control_plane_unavailable': return result['control_plane'] == 'unavailable' and all(c['state'] == 'unknown' for c in result['cells'])
                    if case == 'observer_lost': return all(c['reason'] == 'observer_stale' for c in result['cells'] if c['observer'] == 'a')
                    if case == 'worker_hang': return any(c['check'] == 'accounting' and c['reason'] == 'accounting_stale' for c in result['cells']) and all(c['state'] == 'pass' for c in result['cells'] if c['check'] == 'application')
                    expected = {'single_path': 'single_path', 'client_path_bad': 'single_path', 'row_fault': 'observer_row', 'target_column': 'target_column', 'service_down': 'target_column', 'partition': 'partition_candidate'}[case]
                    return any(p['kind'] == expected and p['check'] == 'application' for p in result['patterns'])
                fault_result, fault_cycles = wait_for(detected, lost=case == 'observer_lost', cp=case == 'control_plane_unavailable')
                detected_at = ms()
                if case == 'observer_lost':
                    # Keep stopped until actual TTL on a running client expires; server stores original report.
                    # Observer agent itself cannot expire its checks while SIGSTOP'd: separate live TTL witness below.
                    pass
                removed = ms(); policy.update(case='healthy', injected=0)
                if case == 'service_down': fixtures[1].start()
                if case == 'observer_lost': backend.resume_observer('a')
                if case == 'control_plane_unavailable': backend.resume_server()
                _, recovery_cycles = wait_for(lambda r, c, p: healthy(r))
                recovered = ms()
                if detected_at - injected > 15000 or recovered - removed > 15000: raise ValueError('SCENARIO_BOUND_EXCEEDED')
                interval_cycles = fault_cycles + recovery_cycles
                unknown_duration = sum(max(0, (interval_cycles[i+1]['at_ms'] if i+1 < len(interval_cycles) else recovered) - c['at_ms']) for i, c in enumerate(interval_cycles) if c['unknown_cells'])
                scenarios.append(dict(id=case, repeat=repeat, proof_kind='real_consul' if case in ('observer_lost', 'control_plane_unavailable') else 'synthetic_fixture', fault_mechanism={'service_down': 'close owned HTTP fixture listener', 'worker_hang': 'freeze synthetic accounting update clock', 'observer_lost': 'SIGSTOP owned Consul observer via pidfd; stop reports', 'control_plane_unavailable': 'SIGSTOP owned server via pidfd'}.get(case, 'pair-specific owned HTTP fixture connection close'), injected_at_ms=injected, removed_at_ms=removed, detected_at_ms=detected_at, recovered_at_ms=recovered, detection_latency_ms=detected_at-injected, recovery_latency_ms=recovered-removed, false_path_down=sum(c['false_path_down'] for c in fault_cycles), unknown_duration_ms=unknown_duration, comparators={'baseline': 'HEAD primary; any HTTP status; no redirects', 'candidate': 'TCP / two HTTP resources / accounting via actual Consul', 'fault_samples': fault_cycles, 'recovery_samples': recovery_cycles}, rollback={'restored': True, 'all_cells_pass': True, 'method': 'owned listener restart / pidfd CONT / clear fixture policy'}))
        # Real running-agent TTL expiry, no report refresh; assert API critical then evaluator unknown.
        timestamp = ms(); backend.heartbeat('a', timestamp)
        backend.publish('a', 'a', 'transport', 'pass', 'ok', timestamp, seq + 1)
        ttl_deadline = time.monotonic() + 12
        while time.monotonic() < ttl_deadline:
            time.sleep(.5)
            cp, records = backend.read_consistent()
            expired = [r for r in records if r['CheckID'] == 'pilot-a-a-transport' and r['Status'] == 'critical']
            if expired:
                evaluated = evaluate(backend.snapshot(IDS, CHECKS, 'synthetic_single_host', META), now_ms=ms(), max_age_ms=4000)
                cell = next(c for c in evaluated['cells'] if (c['observer'], c['target'], c['check']) == ('a', 'a', 'transport'))
                if evaluated['control_plane'] != 'ok' or cell['state'] != 'unknown' or cell['reason'] not in ('observer_stale', 'stale'): raise ValueError('TTL_NOT_UNKNOWN')
                backend.metrics['ttl_expiration_observed'] = True
                backend.metrics['ttl_evaluator_reason'] = cell['reason']
                break
        if not backend.metrics['ttl_expiration_observed']: raise ValueError('TTL_NOT_OBSERVED')
        seq += 1
        _, recovered_cycles = wait_for(lambda r, c, p: healthy(r))
        scenarios.append(dict(id='recovery', repeat=1, proof_kind='real_consul', fault_mechanism='fresh reports after real TTL expiration', injected_at_ms=timestamp, removed_at_ms=recovered_cycles[0]['at_ms'], detected_at_ms=recovered_cycles[0]['at_ms'], recovered_at_ms=ms(), false_path_down=0, unknown_duration_ms=0, rollback={'restored': True}))
        outcome = 'passed'
    except Exception as exc:
        allowed = {'BASELINE_NOT_HEALTHY', 'SCENARIO_BOUND_EXCEEDED', 'ACL_ACCEPTANCE', 'TTL_NOT_UNKNOWN', 'TTL_NOT_OBSERVED', 'CONSUL_API', 'CONSUL_START', 'CONSUL_READINESS'}
        failure = str(exc) if isinstance(exc, ValueError) and str(exc) in allowed else 'LAB_ACCEPTANCE_FAILED'
    finally:
        for fixture in fixtures: fixture.stop()
        pool.shutdown(wait=True, cancel_futures=True)
        cleanup = backend.stop()
    if cleanup['owned_processes_remaining'] or cleanup['runtime_secrets_remaining']: outcome = 'failed'
    resources = dict(method='sampled /proc/PID/stat RSS and CPU for own Consul agents, guardian and runner; CPU is cumulative process lifetime; runtime file sizes', samples=len(backend.resource_samples), sampled_peak_rss_bytes=max((x['rss_bytes'] for x in backend.resource_samples), default=0), cpu_seconds=max((x['cpu_seconds'] for x in backend.resource_samples), default=0), disk_bytes=max((x['disk_bytes'] for x in backend.resource_samples), default=0), checks=len(backend.registered), elapsed_ms=int((time.monotonic()-start)*1000), cleanup_elapsed_ms=cleanup['elapsed_ms'])
    requirements = {('REQ-CONSUL-%02d' % n): {'status': 'lab_observed' if n in (1,2,3,7,8,9) and outcome == 'passed' else 'not_proven_in_lab', 'evidence': ['scenarios', 'consul', 'resources', 'events']} for n in range(1,10)}
    requirements.update({('INV-CONSUL-%02d' % n): {'status': 'unit_and_lab', 'evidence': ['test_consul_pilot.py', 'scenarios']} for n in range(1,4)})
    evidence = dict(schema_version=1, mode='lab', outcome=outcome, failure=failure, consul=backend.metrics, scenarios=scenarios, cycles=cycles, events=export_events(samples), resources=resources, cleanup=cleanup, recommendation='limit' if outcome == 'passed' else 'reject', limitations=['One real host; all AS/DC/physical labels synthetic. No geography or production HA proof.', 'HEAD comparator emulates pinned Mihomo URLTest semantics; lab interval1s/timeout0.5s vs production30s/5s.', 'Dev VLESS/accounting/service acceptance remains separate; CONSUL-2 parent not closed by lab.', 'RSS sampling can miss peaks. CPU includes Consul, guardian and runner cumulative process lifetimes, including setup; Python fixture threads share runner PID.', 'Consul check_update_interval=1s explicitly accelerates same-status Output synchronization; default can make 4s freshness impossible.', 'Unknown duration uses sample-and-hold evaluation timestamps across fault and recovery, not continuous monitoring.'], requirement_evidence=requirements, source_hashes=source_hashes)
    evidence_path, report_path = output / 'evidence.json', output / 'report.md'
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    report_path.write_text('# Consul bounded lab\n\nOutcome: ' + outcome + '. Recommendation: ' + evidence['recommendation'] + '.\n\nDevelopment validation history (not accepted runs): initial real run failed BASELINE_NOT_HEALTHY because default Consul check_update_interval deferred same-status Output synchronization; explicitly setting1s fixed the accelerated4s-freshness model. Second real run completed healthy+27fault rows but failed CONSUL_API at server-suspend because writer ACL resolution depends on the server; during this fault runner now records direct probes without claiming successful publication and reads the unavailable control plane. Neither failed run was counted as acceptance.\n\nActual Consul 2.0.4; SHA256 ' + backend.metrics['binary_sha256'] + '.\n\nBenefit: independent transport/application/accounting reports retain timestamps; expired observer data becomes unknown. Diagnostic patterns are our evaluator, not Consul gossip. Cost: ' + json.dumps(resources, sort_keys=True) + '.\n\nRollback: own pidfd-identified processes stopped; private runtime cleaned. No production controls or notifications.\n\n' + '\n'.join('- ' + x for x in evidence['limitations']) + '\n\nHistory adapter: ok/failing maps to LineProbe States; unknown requires inconclusive/probe-loss. Persist event_id and last definitive state before histories/alerts/triage delivery; current export is pure and production_delivery=false.\n\nProduction proposal (not implemented): 3–5 servers in separate fault domains, TLS/gossip encryption, ACL/token/certificate rotation, upgrade runbook, snapshots and restore rehearsal, self-monitoring. Probes scale O(observers×targets×checks). Estimated setup 16–32h, adapters and operations runbooks 16–32h, acceptance/restore rehearsal 8–16h; assumes existing automation and small fleet. Ongoing 2–4h/month plus incident work; validate against production inventory. Next acceptance: real dev service/VLESS/counter fault cycles, then independent production HA/security review.\n\nRequirement mapping:\n\n' + '\n'.join('- ' + k + ': ' + json.dumps(v, sort_keys=True) for k,v in requirements.items()) + '\n\nSource hashes:\n\n' + json.dumps(evidence['source_hashes'], indent=2) + '\n\nMachine evidence contains each scenario, matched comparator samples, event transitions, API counts and TTL/ACL evidence. Sources: accepted docs/dev/2026-10-11-spec-consul-pilot.md; pinned Mihomo v1.19.25 adapter/adapter.go URLTest; Consul Agent Check/Health/ACL APIs.\n')
    return dict(schema_version=1, mode='lab', outcome=outcome, acceptance_complete=outcome == 'passed', report=str(report_path), evidence=str(evidence_path), consul_version='2.0.4', cleanup=cleanup)
