"""Real Consul acceptance. CONSUL_BIN is mandatory; never download or substitute a stub."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from test_consul_pilot import load_contract

PIN = '9f80affeb492d5e2d8d6c8626107666923e1935ec41c1a3fc6ebcce8846368ec'


class RealConsulTests(unittest.TestCase):
    def test_real_lab_api_ttl_acl_scenarios_and_cleanup(self):
        module = load_contract(self)
        binary = os.environ.get('CONSUL_BIN')
        self.assertTrue(binary, 'Set explicit CONSUL_BIN to pinned real Consul 2.0.4')
        path = Path(binary)
        self.assertTrue(path.is_absolute() and path.is_file())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), PIN)
        # Protected, nonsynced temporary runtime; the runner owns only this empty directory.
        with tempfile.TemporaryDirectory(prefix='consul-contract-', dir='/tmp') as base:
            root = Path(base); runtime = root / 'runtime'; runtime.mkdir(mode=0o700)
            output = root / 'output'; output.mkdir(mode=0o700)
            result = module.run_lab(consul_binary=str(path), runtime_dir=str(runtime), output_dir=str(output))
            self.assertEqual(result['schema_version'], 1)
            self.assertEqual(result['mode'], 'lab')
            self.assertEqual(result['outcome'], 'passed')
            self.assertIs(result['acceptance_complete'], True)
            self.assertEqual(result['consul_version'], '2.0.4')
            self.assertEqual(result['cleanup']['owned_processes_remaining'], 0)
            self.assertEqual(result['cleanup']['runtime_secrets_remaining'], 0)
            for key in ('report', 'evidence'):
                artifact = Path(result[key])
                self.assertTrue(artifact.is_absolute() and artifact.is_file())
                self.assertTrue(artifact.is_relative_to(output))
            evidence = json.loads(Path(result['evidence']).read_text())
            consul = evidence['consul']
            self.assertEqual(consul['version'], '2.0.4')
            self.assertEqual(consul['binary_sha256'], PIN)
            self.assertGreater(consul['api_reads'], 0)
            self.assertGreater(consul['api_writes'], 0)
            self.assertIs(consul['ttl_expiration_observed'], True)
            self.assertEqual(consul['acl_denials']['anonymous_write'], 403)
            self.assertEqual(consul['acl_denials']['evaluator_write'], 403)
            scenarios = evidence['scenarios']
            self.assertIsInstance(evidence['events'], list)
            for name in ('control_point_only', 'single_path', 'row_fault', 'service_down',
                         'worker_hang', 'observer_lost', 'partition', 'control_plane_unavailable'):
                rows = [s for s in scenarios if s['id'] == name]
                self.assertEqual({r['repeat'] for r in rows}, {1, 2, 3}, name)
                for row in rows:
                    self.assertIn(row['proof_kind'], ('real_consul', 'synthetic_fixture'))
                    self.assertIsInstance(row['rollback'], dict)
                    for start, end in [('injected_at_ms', 'detected_at_ms'), ('removed_at_ms', 'recovered_at_ms')]:
                        self.assertIsInstance(row[end], int, name)
                        self.assertGreaterEqual(row[end] - row[start], 0)
                        self.assertLessEqual(row[end] - row[start], 15000)
            self.assertIn('healthy', {s['id'] for s in scenarios})
            self.assertIn('recovery', {s['id'] for s in scenarios})
            self.assertIn(evidence['recommendation'], ('integrate', 'limit', 'reject'))
            self.assertIsInstance(evidence['resources'], dict)
            self.assertIsInstance(evidence['limitations'], list)
            for req in ['REQ-CONSUL-%02d' % n for n in range(1, 10)] + ['INV-CONSUL-%02d' % n for n in range(1, 4)]:
                self.assertIn(req, evidence['requirement_evidence'])


if __name__ == '__main__':
    unittest.main()
