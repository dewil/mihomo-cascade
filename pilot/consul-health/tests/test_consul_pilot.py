"""Blind contract tests from accepted CONSUL-2 spec; no implementation imports at collection."""
import copy
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'consul_pilot.py'
NOW = 10000


def load_contract(case):
    case.assertTrue(MODULE.is_file(), 'Missing public feature: consul_pilot.py')
    spec = importlib.util.spec_from_file_location('consul_pilot_contract', MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for name in ('evaluate', 'combine_application', 'run_lab', 'run_dev'):
        case.assertTrue(callable(getattr(module, name, None)), 'Missing public callable: ' + name)
    return module


def snapshot(ids=('a', 'b', 'c'), checks=('application',)):
    return dict(schema_version=1, control_plane='ok', topology_kind='real',
                expected_checks=list(checks),
                observers=[dict(id=x, asn='as_' + x, dc='dc_' + x,
                                physical_domain='host_' + x, last_seen_ms=NOW) for x in ids],
                targets=[dict(id=x) for x in ids],
                observations=[dict(observer=o, target=t, check=c, observed_at_ms=NOW,
                                   seq=1, state='pass', reason='ok')
                              for o, t, c in itertools.product(ids, ids, checks)])


def mark(s, observer, target, state='fail', reason='timeout', check='application'):
    row = next(x for x in s['observations'] if
               (x['observer'], x['target'], x['check']) == (observer, target, check))
    row.update(state=state, reason=reason)
    return row


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.module = load_contract(self)

    def evaluate(self, s, **kwargs):
        return self.module.evaluate(s, now_ms=kwargs.get('now_ms', NOW),
                                    max_age_ms=kwargs.get('max_age_ms', 4000))

    def test_healthy_exact_cells_and_no_patterns(self):
        s = snapshot()
        result = self.evaluate(s)
        expected = [dict(observer=o, target=t, check='application', state='pass',
                         reason='ok', observed_at_ms=NOW, seq=1)
                    for o, t in itertools.product('abc', repeat=2)]
        self.assertEqual(result, dict(schema_version=1, control_plane='ok',
                         topology_kind='real', evaluated_at_ms=NOW, cells=expected,
                         witness_groups=[['a'], ['b'], ['c']], patterns=[], fatal_cause=None))

    def test_missing_catalog_full_cross_product(self):
        s = snapshot(checks=('transport', 'application', 'accounting'))
        s['observations'] = []
        r = self.evaluate(s)
        self.assertEqual(len(r['cells']), 27)
        self.assertTrue(all((x['state'], x['reason'], x['seq'], x['observed_at_ms']) ==
                            ('unknown', 'missing', None, None) for x in r['cells']))

    def test_control_plane_overrides_and_has_no_patterns(self):
        for cp, reason in [('unavailable', 'control_plane_unavailable'),
                           ('acl_denied', 'acl_denied'), ('partial', 'partial_catalog')]:
            with self.subTest(cp=cp):
                s = snapshot(); s['control_plane'] = cp
                r = self.evaluate(s)
                self.assertEqual(r['patterns'], [])
                self.assertTrue(all((x['state'], x['reason'], x['seq'], x['observed_at_ms']) ==
                                    ('unknown', reason, None, None) for x in r['cells']))

    def test_heartbeat_missing_future_stale_and_exact_boundary(self):
        for timestamp, reason in [(None, 'observer_missing'), (NOW + 1, 'observer_clock_skew'),
                                  (5999, 'observer_stale'), (6000, 'ok')]:
            s = snapshot(); s['observers'][0]['last_seen_ms'] = timestamp
            cells = [c for c in self.evaluate(s)['cells'] if c['observer'] == 'a']
            self.assertEqual({c['reason'] for c in cells}, {reason})
            if reason != 'ok':
                self.assertTrue(all(c['seq'] is None and c['observed_at_ms'] is None for c in cells))

    def test_observation_time_and_metadata(self):
        for timestamp, state, reason in [(6000, 'pass', 'ok'), (5999, 'unknown', 'stale'),
                                         (10001, 'unknown', 'clock_skew')]:
            s = snapshot(); s['observations'][0]['observed_at_ms'] = timestamp
            cell = self.evaluate(s)['cells'][0]
            self.assertEqual((cell['state'], cell['reason'], cell['observed_at_ms'], cell['seq']),
                             (state, reason, timestamp, 1))

    def test_seq_replay_duplicates_conflict_and_stale_winner(self):
        s = snapshot(); original = copy.deepcopy(s['observations'][0])
        s['observations'].extend([copy.deepcopy(original), dict(original, seq=0, state='fail', reason='timeout')])
        self.assertEqual(self.evaluate(s)['cells'][0]['state'], 'pass')
        s['observations'].append(dict(original, reason='timeout', state='fail'))
        cell = self.evaluate(s)['cells'][0]
        self.assertEqual((cell['state'], cell['reason'], cell['seq'], cell['observed_at_ms']),
                         ('unknown', 'sequence_conflict', None, None))
        s['observations'].append(dict(original, seq=2, observed_at_ms=0))
        self.assertEqual(self.evaluate(s)['cells'][0]['reason'], 'stale')

    def test_witness_transitive_correlation_and_unknown(self):
        s = snapshot(('a', 'b', 'c', 'd'))
        s['observers'][1]['asn'] = s['observers'][0]['asn']
        s['observers'][2]['dc'] = s['observers'][1]['dc']
        self.assertEqual(self.evaluate(s)['witness_groups'], [['a', 'b', 'c'], ['d']])
        s['observers'][3]['physical_domain'] = None
        self.assertEqual(self.evaluate(s)['witness_groups'], [['a', 'b', 'c', 'd']])

    def test_single_path_exact_evidence(self):
        s = snapshot(); mark(s, 'a', 'b')
        self.assertEqual(self.evaluate(s)['patterns'], [dict(kind='single_path', check='application',
            observers=['a'], targets=['b'], evidence=['a/a/application', 'a/b/application',
            'a/c/application', 'b/b/application', 'c/b/application'], proof_kind='observed', independent_groups=1)])

    def test_row_and_column_coexist(self):
        s = snapshot(); mark(s, 'a', 'b'); mark(s, 'a', 'c'); mark(s, 'b', 'c')
        patterns = self.evaluate(s)['patterns']
        row = next(p for p in patterns if p['kind'] == 'observer_row')
        column = next(p for p in patterns if p['kind'] == 'target_column')
        self.assertEqual((row['observers'], row['targets']), (['a'], ['b', 'c']))
        self.assertEqual((column['observers'], column['targets'], column['independent_groups']), (['a', 'b'], ['c'], 2))
        s['observers'][1]['physical_domain'] = s['observers'][0]['physical_domain']
        self.assertNotIn('target_column', [p['kind'] for p in self.evaluate(s)['patterns']])

    def test_column_requires_control_pass_unknown_does_not_vote(self):
        s = snapshot(); mark(s, 'a', 'c'); mark(s, 'b', 'c')
        for target in ('a', 'b'):
            mark(s, 'b', target, 'unknown', 'probe_error')
        self.assertEqual([p['kind'] for p in self.evaluate(s)['patterns']], ['insufficient_evidence'])

    def test_partition_all_cells_and_synthetic_proof(self):
        s = snapshot(('a', 'b', 'c', 'd')); s['topology_kind'] = 'synthetic_single_host'
        for row in s['observations']:
            if (row['observer'] in 'ab') != (row['target'] in 'ab'):
                row.update(state='fail', reason='timeout')
        p = next(p for p in self.evaluate(s)['patterns'] if p['kind'] == 'partition_candidate')
        self.assertEqual(p['observers'], list('abcd'))
        self.assertEqual(p['targets'], list('abcd'))
        self.assertEqual(len(p['evidence']), 16)
        self.assertEqual(p['proof_kind'], 'synthetic')
        mark(s, 'a', 'c', 'unknown', 'missing')
        self.assertNotIn('partition_candidate', [p['kind'] for p in self.evaluate(s)['patterns']])

    def test_check_independence_and_determinism(self):
        s = snapshot(checks=('transport', 'application', 'accounting'))
        mark(s, 'a', 'b'); mark(s, 'b', 'a', 'unknown', 'missing', 'accounting')
        before = copy.deepcopy(s); r = self.evaluate(s)
        self.assertEqual(s, before)
        self.assertEqual({p['check'] for p in r['patterns']}, {'application', 'accounting'})
        for key in ('observers', 'targets', 'observations', 'expected_checks'):
            s[key].reverse()
        self.assertEqual(self.evaluate(s), r)

    def test_structural_errors_constant_and_no_value_leak(self):
        cases = []
        for key in snapshot():
            s = snapshot(); del s[key]; cases.append(s)
        for key, value in [('schema_version', True), ('control_plane', 'PRIVATE_SENTINEL'),
                           ('topology_kind', 'bad'), ('expected_checks', ['transport', 'transport']),
                           ('observers', []), ('targets', []), ('extra', 'PRIVATE_SENTINEL')]:
            s = snapshot(); s[key] = value; cases.append(s)
        for key, value in [('seq', True), ('seq', -1), ('observed_at_ms', 1.5),
                           ('observer', 'absent'), ('check', 'other'), ('reason', 'PRIVATE_SENTINEL'),
                           ('state', 'ok')]:
            s = snapshot(); s['observations'][0][key] = value; cases.append(s)
        for key in ('asn', 'dc', 'physical_domain', 'last_seen_ms'):
            s = snapshot(); del s['observers'][0][key]; cases.append(s)
        s = snapshot(); s['observers'].append(copy.deepcopy(s['observers'][0])); cases.append(s)
        s = snapshot(); s['observers'][0]['id'] = 'é'; cases.append(s)
        for s in cases:
            with self.subTest(case=cases.index(s)):
                with self.assertRaises(ValueError) as caught: self.evaluate(s)
                self.assertNotIn('PRIVATE_SENTINEL', str(caught.exception))
                self.assertRegex(str(caught.exception), r'^[A-Za-z0-9_-]+$')
        for kwargs in ({'now_ms': True}, {'now_ms': -1}, {'max_age_ms': 0}, {'max_age_ms': float('inf')}):
            with self.assertRaises(ValueError): self.evaluate(snapshot(), **kwargs)

    def test_combiner_full_truth_table(self):
        values = {'pass': 'ok', 'fail': 'timeout', 'unknown': 'probe_error'}
        for a, b in itertools.product(values, repeat=2):
            result = self.module.combine_application(dict(state=a, reason=values[a]), dict(state=b, reason=values[b]))
            expected = ('pass', 'ok') if 'pass' in (a, b) else (
                ('fail', 'all_endpoints_failed') if a == b == 'fail' else ('unknown', 'probe_error'))
            self.assertEqual(result, dict(zip(('state', 'reason'), expected)))
        s = snapshot(); mark(s, 'a', 'b', reason='all_endpoints_failed')
        self.assertEqual(self.evaluate(s)['cells'][1]['state'], 'fail')

    def test_cli_rejects_duplicate_keys_oversize_and_unknown_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.json'
            for content in ('{"schema_version":1,"schema_version":1,"private":"PRIVATE_SENTINEL"}',
                            ' ' * (1024 * 1024 + 1), '{"private":"PRIVATE_SENTINEL"}'):
                path.write_text(content)
                p = subprocess.run([sys.executable, str(MODULE), 'evaluate', '--input', str(path),
                                    '--now-ms', str(NOW), '--max-age-ms', '4000'], capture_output=True, text=True)
                self.assertEqual(p.returncode, 2)
                self.assertIsInstance(json.loads(p.stdout), dict)
                self.assertNotIn('PRIVATE_SENTINEL', p.stdout + p.stderr)
            p = subprocess.run([sys.executable, str(MODULE), 'evaluate', '--unknown-flag'], capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)

    def test_snapshot_size_limit(self):
        s = snapshot(tuple('n' + str(i) for i in range(65)))
        with self.assertRaises(ValueError): self.evaluate(s)

    def test_dev_unsafe_arguments_never_touch_network_or_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            for host, node, flag in [('evil.invalid', 42, True), ('de4.cactushub.app', 43, True),
                                     ('de4.cactushub.app', 42, 'yes')]:
                with self.subTest(host=host, node=node, flag=flag):
                    with patch('subprocess.Popen', side_effect=AssertionError('unsafe subprocess')), \
                         patch('socket.socket', side_effect=AssertionError('unsafe network')):
                        try:
                            result = self.module.run_dev(consul_binary='/nonexistent/consul', runtime_dir=d,
                                output_dir=d, target_host=host, dev_node_id=node, allow_dev_faults=flag)
                        except ValueError:
                            continue
                        self.assertFalse(result.get('acceptance_complete', False))
                        self.assertNotEqual(result.get('outcome'), 'passed')


class EventContractTests(unittest.TestCase):
    def setUp(self):
        self.module = load_contract(self)
        self.assertTrue(callable(getattr(self.module, 'export_events', None)),
                        'Missing public callable: export_events')

    def sample(self, time, state, subject='consul-pilot/a/b/application', **kwargs):
        sample = dict(subject=subject, measured_at_ms=time, state=state,
                      reason={'ok': 'ok', 'failing': 'timeout', 'unknown': 'stale'}[state],
                      evidence_ids=['z', 'a', 'z'])
        sample.update(kwargs)
        return sample

    def test_events_unknown_preserves_failure_and_exact_canonical_hash(self):
        samples = [self.sample(i, state) for i, state in enumerate(
            ('ok', 'failing', 'unknown', 'unknown', 'ok', 'ok', 'failing'))]
        before = copy.deepcopy(samples)
        events = self.module.export_events(samples)
        self.assertEqual(samples, before)
        transitions = ['observation', 'problem', 'unknown', 'unknown', 'recovery', 'observation', 'problem']
        self.assertEqual([e['transition'] for e in events], transitions)
        for sample, transition, actual in zip(samples, transitions, events):
            expected = dict(schema_version=1, subject=sample['subject'], measured_at_ms=sample['measured_at_ms'],
                            state=sample['state'], transition=transition, reason=sample['reason'],
                            evidence_ids=['a', 'z'], origin='consul-pilot', production_delivery=False)
            canonical = json.dumps(expected, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
            expected['event_id'] = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
            self.assertEqual(actual, expected)

    def test_events_stale_replay_dedup_and_subject_isolation(self):
        a = self.sample(20, 'failing')
        duplicate = dict(a, evidence_ids=['a', 'z'])
        samples = [a, duplicate, self.sample(19, 'ok'),
                   self.sample(1, 'ok', subject='consul-pilot/c/b/application'),
                   self.sample(21, 'unknown'), self.sample(22, 'ok')]
        events = self.module.export_events(samples)
        self.assertEqual([e['measured_at_ms'] for e in events], [20, 1, 21, 22])
        self.assertEqual([e['transition'] for e in events], ['problem', 'observation', 'unknown', 'recovery'])
        self.assertEqual(len({e['event_id'] for e in events}), 4)
        self.assertEqual(self.module.export_events([]), [])

    def test_events_equal_timestamp_conflicts_fail_closed(self):
        base = self.sample(1, 'failing')
        for change in (dict(state='ok', reason='ok'), dict(reason='connect_failed'), dict(evidence_ids=['different'])):
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, '^EVENT_CONFLICT$'):
                    self.module.export_events([base, dict(base, **change)])

    def test_events_schema_rejects_unknown_fields_types_and_contradictions(self):
        cases = []
        for key in self.sample(1, 'ok'):
            sample = self.sample(1, 'ok'); del sample[key]; cases.append(sample)
        for key, value in [('extra', 'PRIVATE_SENTINEL'), ('subject', 'PRIVATE_SENTINEL'),
                           ('measured_at_ms', True), ('measured_at_ms', -1), ('measured_at_ms', 1.5),
                           ('state', 'pass'), ('reason', 'timeout'), ('evidence_ids', 'PRIVATE_SENTINEL'),
                           ('evidence_ids', [1])]:
            sample = self.sample(1, 'ok'); sample[key] = value; cases.append(sample)
        for sample in cases:
            with self.assertRaises(ValueError) as caught:
                self.module.export_events([sample])
            self.assertNotIn('PRIVATE_SENTINEL', str(caught.exception))
            self.assertRegex(str(caught.exception), r'^[A-Za-z0-9_-]+$')


if __name__ == '__main__':
    unittest.main()
