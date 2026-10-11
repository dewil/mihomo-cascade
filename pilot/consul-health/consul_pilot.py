#!/usr/bin/env python3
"""Bounded Consul pilot: pure typed evaluator and explicit opt-in runners."""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import re
import sys

# Importable by file path as well as executable as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
CHECKS = {'transport', 'application', 'accounting'}
FAIL = {'connect_failed', 'timeout', 'http_status', 'body_mismatch', 'accounting_stale', 'all_endpoints_failed'}
UNKNOWN = {'probe_error', 'missing', 'invalid_report'}
OUTPUT_UNKNOWN = UNKNOWN | {'control_plane_unavailable', 'acl_denied', 'partial_catalog', 'observer_missing', 'observer_clock_skew', 'observer_stale', 'sequence_conflict', 'clock_skew', 'stale'}
ID = re.compile(r'[A-Za-z0-9_-]+\Z')
LIMIT = 1024 * 1024


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def fields(value, keys):
    if type(value) is not dict or set(value) != set(keys.split()):
        raise ValueError('INVALID_SCHEMA')


def pair(value):
    fields(value, 'state reason')
    state, reason = value['state'], value['reason']
    if type(state) is not str or type(reason) is not str or reason not in {'pass': {'ok'}, 'fail': FAIL, 'unknown': UNKNOWN}.get(state, set()):
        raise ValueError('INVALID_STATE')


def decode_json(data):
    if len(data) > LIMIT:
        raise ValueError('INPUT_TOO_LARGE')
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('DUPLICATE_KEY')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('INVALID_JSON')))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError('INVALID_JSON') from None


def validate(snapshot, now_ms, max_age_ms):
    if not integer(now_ms) or not integer(max_age_ms, 1):
        raise ValueError('INVALID_TIME')
    fields(snapshot, 'schema_version control_plane topology_kind expected_checks observers targets observations')
    try:
        size = len(json.dumps(snapshot, ensure_ascii=False).encode())
    except (TypeError, OverflowError, RecursionError, ValueError):
        raise ValueError('INVALID_SCHEMA') from None
    if size > LIMIT:
        raise ValueError('INPUT_TOO_LARGE')
    if type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1:
        raise ValueError('INVALID_VERSION')
    if snapshot['control_plane'] not in ('ok', 'unavailable', 'acl_denied', 'partial') or snapshot['topology_kind'] not in ('real', 'synthetic_single_host'):
        raise ValueError('INVALID_SCHEMA')
    checks = snapshot['expected_checks']
    if type(checks) is not list or not checks or any(type(x) is not str or x not in CHECKS for x in checks) or len(set(checks)) != len(checks):
        raise ValueError('INVALID_CHECKS')
    ids = []
    for group, keys in [('observers', 'id asn dc physical_domain last_seen_ms'), ('targets', 'id')]:
        rows = snapshot[group]
        if type(rows) is not list or not 1 <= len(rows) <= 64:
            raise ValueError('INVALID_TOPOLOGY')
        names = set()
        for row in rows:
            fields(row, keys)
            name = row['id']
            if type(name) is not str or not ID.fullmatch(name) or name in names:
                raise ValueError('INVALID_ID')
            names.add(name)
            if group == 'observers':
                if any(row[k] is not None and type(row[k]) is not str for k in ('asn', 'dc', 'physical_domain')):
                    raise ValueError('INVALID_DOMAIN')
                if row['last_seen_ms'] is not None and not integer(row['last_seen_ms']):
                    raise ValueError('INVALID_TIME')
        ids.append(names)
    if type(snapshot['observations']) is not list:
        raise ValueError('INVALID_OBSERVATIONS')
    for row in snapshot['observations']:
        fields(row, 'observer target check observed_at_ms seq state reason')
        if any(type(row[k]) is not str for k in ('observer', 'target', 'check')) or row['observer'] not in ids[0] or row['target'] not in ids[1] or row['check'] not in checks:
            raise ValueError('INVALID_REFERENCE')
        if not integer(row['observed_at_ms']) or not integer(row['seq']):
            raise ValueError('INVALID_TIME')
        pair({k: row[k] for k in ('state', 'reason')})


def evaluate(snapshot, *, now_ms, max_age_ms):
    validate(snapshot, now_ms, max_age_ms)
    observers = {o['id']: o for o in snapshot['observers']}
    targets = sorted(t['id'] for t in snapshot['targets'])
    checks = sorted(snapshot['expected_checks'])
    groups = [{o} for o in sorted(observers)]
    for a, b in itertools.combinations(sorted(observers), 2):
        left, right = observers[a], observers[b]
        keys = ('asn', 'dc', 'physical_domain')
        if any(left[k] is None or right[k] is None or left[k] == right[k] for k in keys):
            ga = next(g for g in groups if a in g)
            gb = next(g for g in groups if b in g)
            if ga is not gb:
                ga.update(gb); groups.remove(gb)
    groups = sorted(sorted(g) for g in groups)
    def count(names):
        return sum(bool(set(g) & set(names)) for g in groups)
    records = {}
    for row in snapshot['observations']:
        records.setdefault((row['observer'], row['target'], row['check']), []).append(row)
    cells = []
    for o, t, c in itertools.product(sorted(observers), targets, checks):
        cell = dict(observer=o, target=t, check=c, state='unknown', reason='missing', observed_at_ms=None, seq=None)
        cp = snapshot['control_plane']
        heartbeat = observers[o]['last_seen_ms']
        if cp != 'ok':
            cell['reason'] = {'unavailable': 'control_plane_unavailable', 'acl_denied': 'acl_denied', 'partial': 'partial_catalog'}[cp]
        elif heartbeat is None:
            cell['reason'] = 'observer_missing'
        elif heartbeat > now_ms:
            cell['reason'] = 'observer_clock_skew'
        elif now_ms - heartbeat > max_age_ms:
            cell['reason'] = 'observer_stale'
        elif records.get((o, t, c)):
            rows = records[o, t, c]
            seq = max(r['seq'] for r in rows)
            winners = {(r['observed_at_ms'], r['state'], r['reason']) for r in rows if r['seq'] == seq}
            if len(winners) != 1:
                cell['reason'] = 'sequence_conflict'
            else:
                timestamp, state, reason = next(iter(winners))
                cell.update(observed_at_ms=timestamp, seq=seq)
                if timestamp > now_ms:
                    cell['reason'] = 'clock_skew'
                elif now_ms - timestamp > max_age_ms:
                    cell['reason'] = 'stale'
                else:
                    cell.update(state=state, reason=reason)
        cells.append(cell)
    patterns = []
    def add(kind, check, os, ts, evidence, **extra):
        patterns.append(dict(kind=kind, check=check, observers=sorted(set(os)), targets=sorted(set(ts)), evidence=sorted(set('/'.join((x['observer'], x['target'], x['check'])) for x in evidence)), proof_kind='synthetic' if snapshot['topology_kind'] == 'synthetic_single_host' else 'observed', independent_groups=count(os), **extra))
    if snapshot['control_plane'] == 'ok':
        for check in checks:
            matrix = [x for x in cells if x['check'] == check]
            fails = [x for x in matrix if x['state'] == 'fail']
            passes = [x for x in matrix if x['state'] == 'pass']
            start = len(patterns)
            if len(fails) == 1:
                f = fails[0]
                rp = [x for x in passes if x['observer'] == f['observer'] and x['target'] != f['target']]
                cp = [x for x in passes if x['target'] == f['target'] and x['observer'] != f['observer']]
                if rp and cp:
                    add('single_path', check, [f['observer']], [f['target']], fails + rp + cp)
            for o in sorted(observers):
                fs = [x for x in fails if x['observer'] == o]
                ps = [x for x in passes if x['observer'] != o and x['target'] in {f['target'] for f in fs}]
                if len(fs) >= 2 and {x['target'] for x in ps} == {x['target'] for x in fs}:
                    add('observer_row', check, [o], [x['target'] for x in fs], fs + ps)
            for t in targets:
                fs = [x for x in fails if x['target'] == t and any(p['observer'] == x['observer'] and p['target'] != t for p in passes)]
                os = [x['observer'] for x in fs]
                if count(os) >= 2:
                    add('target_column', check, os, [t], fs + [p for p in passes if p['observer'] in os and p['target'] != t])
            if set(observers) == set(targets):
                first = min(observers)
                block = {x['target'] for x in matrix if x['observer'] == first and x['state'] == 'pass'}
                other = set(observers) - block
                if first in block and other and all(x['state'] == ('pass' if (x['observer'] in block) == (x['target'] in block) else 'fail') for x in matrix):
                    add('partition_candidate', check, observers, targets, matrix, blocks=[sorted(block), sorted(other)])
            if len(patterns) == start:
                nonpass = [x for x in matrix if x['state'] != 'pass']
                if nonpass:
                    add('insufficient_evidence', check, [x['observer'] for x in nonpass], [x['target'] for x in nonpass], nonpass)
    patterns.sort(key=lambda p: (p['kind'], p['check'], ','.join(p['observers']), ','.join(p['targets'])))
    return dict(schema_version=1, control_plane=snapshot['control_plane'], topology_kind=snapshot['topology_kind'], evaluated_at_ms=now_ms, cells=cells, witness_groups=groups, patterns=patterns, fatal_cause=None)


def combine_application(primary, secondary):
    pair(primary); pair(secondary)
    states = [primary['state'], secondary['state']]
    state, reason = ('pass', 'ok') if 'pass' in states else (('fail', 'all_endpoints_failed') if states == ['fail', 'fail'] else ('unknown', 'probe_error'))
    return dict(state=state, reason=reason)


def export_events(samples):
    if type(samples) is not list:
        raise ValueError('INVALID_EVENTS')
    result, latest, states, seen = [], {}, {}, {}
    for sample in samples:
        fields(sample, 'subject measured_at_ms state reason evidence_ids')
        subject, timestamp, state, reason = (sample[k] for k in ('subject', 'measured_at_ms', 'state', 'reason'))
        if type(subject) is not str or not re.fullmatch(r'consul-pilot/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/(transport|application|accounting)', subject) or not integer(timestamp):
            raise ValueError('INVALID_EVENT')
        if type(state) is not str or type(reason) is not str or reason not in {'ok': {'ok'}, 'failing': FAIL, 'unknown': OUTPUT_UNKNOWN}.get(state, set()):
            raise ValueError('INVALID_EVENT')
        evidence = sample['evidence_ids']
        if type(evidence) is not list or any(type(x) is not str for x in evidence):
            raise ValueError('INVALID_EVENT')
        normalized = dict(sample, evidence_ids=sorted(set(evidence)))
        if timestamp < latest.get(subject, -1):
            continue
        key = (subject, timestamp)
        if key in seen:
            if seen[key] != normalized:
                raise ValueError('EVENT_CONFLICT')
            continue
        seen[key] = normalized
        latest[subject] = timestamp
        transition = 'unknown' if state == 'unknown' else ('problem' if state == 'failing' and states.get(subject) != 'failing' else ('recovery' if state == 'ok' and states.get(subject) == 'failing' else 'observation'))
        if state != 'unknown':
            states[subject] = state
        event = dict(schema_version=1, **normalized, transition=transition, origin='consul-pilot', production_delivery=False)
        event['event_id'] = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        result.append(event)
    return result


def run_lab(*, consul_binary, runtime_dir, output_dir):
    from lab_runner import run_lab as execute
    return execute(consul_binary=consul_binary, runtime_dir=runtime_dir, output_dir=output_dir)


def run_dev(*, consul_binary, runtime_dir, output_dir, target_host, dev_node_id, allow_dev_faults=False):
    if target_host != 'de4.cactushub.app' or type(dev_node_id) is not int or dev_node_id != 42 or type(allow_dev_faults) is not bool:
        raise ValueError('DEV_ALLOWLIST')
    from dev_runner import run_dev as execute
    return execute(consul_binary=consul_binary, runtime_dir=runtime_dir, output_dir=output_dir, target_host=target_host, dev_node_id=dev_node_id, allow_dev_faults=allow_dev_faults)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('INVALID_ARGUMENTS')


def main():
    parser = Parser(add_help=False)
    sub = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
    evaluate_parser = sub.add_parser('evaluate', add_help=False)
    for name, kind in [('input', str), ('now-ms', int), ('max-age-ms', int)]:
        evaluate_parser.add_argument('--' + name, type=kind, required=True)
    for command in ('run_lab', 'run_dev'):
        p = sub.add_parser(command, add_help=False)
        for name in ('consul-bin', 'runtime-dir', 'output-dir'):
            p.add_argument('--' + name, required=True)
        if command == 'run_dev':
            p.add_argument('--target-host', required=True)
            p.add_argument('--dev-node-id', required=True, type=int)
            p.add_argument('--allow-dev-faults', action='store_true')
    try:
        args = vars(parser.parse_args())
        command = args.pop('command')
        if command == 'evaluate':
            with open(args.pop('input'), 'rb') as source:
                data = decode_json(source.read(LIMIT + 1))
            result = evaluate(data, **args)
            code = 0
        else:
            args['consul_binary'] = args.pop('consul_bin')
            result = globals()[command](**args)
            code = 0 if result['outcome'] == 'passed' else 1
    except KeyboardInterrupt:
        result, code = {'error': 'RUN_INTERRUPTED'}, 1
    except (ValueError, OSError, ImportError):
        result, code = {'error': 'PREFLIGHT_OR_CONTRACT_ERROR'}, 2
    except Exception:
        result, code = {'error': 'RUN_FAILED'}, 1
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
