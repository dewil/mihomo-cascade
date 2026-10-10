#!/usr/bin/env python3
"""CASC-FAILOVER: black-box CLI contract from feature specification.

Requires PyYAML for inspecting emitted config, not for running the builder.
Targets: reversed priority, recursive groups, silent malformed metadata,
missing-primary orphan false positive, and diagnostic fail-open.
"""
import base64
import importlib.util
import json
from pathlib import Path
import unittest
from urllib.parse import quote
import yaml

spec = importlib.util.spec_from_file_location('routing_fixture', Path(__file__).with_name('routing-rules-required.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class CascadeFailover(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.RoutingRulesRequired()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.nodes(['ee', 'pl', 'de'])

    def nodes(self, aliases):
        flags = {'ee': '🇪🇪', 'pl': '🇵🇱', 'de': '🇩🇪'}
        lines = ['vless://11111111-2222-3333-4444-555555555555@'
                 f'{a}.example.net:443?security=tls&type=grpc&sni={a}.example.net&serviceName=ABC&encryption=none#'
                 + quote(f'{flags[a]} {a.upper()} direct node') for a in aliases]
        self.fixture.subscription.write_bytes(base64.b64encode(('\n'.join(lines) + '\n').encode()))

    def rules(self, mapping=None, probes=None, extra=''):
        mapping = {'ee': ['pl', 'de'], 'pl': ['de']} if mapping is None else mapping
        probes = ['ee'] if probes is None else probes
        self.fixture.rules('cascade-fallbacks: ' + json.dumps(mapping) + '\n'
                           + 'cascade-probes: ' + json.dumps(probes) + '\n'
                           + 'rules:\n  - "DOMAIN-SUFFIX,example.org,ee-failover"\n'
                           + '  - "DOMAIN,canary.example.org,ee"\n' + extra
                           + '  - "MATCH,DIRECT"\n')

    def build(self):
        return self.fixture.build()

    def config(self, result):
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return yaml.safe_load((self.fixture.base / 'config.yaml').read_text())

    def groups(self, config):
        return {g['name']: g for g in config['proxy-groups']}

    def test_ordered_physical_members_no_recursion_and_active_checks(self):
        self.rules()
        config = self.config(self.build())
        group = self.groups(config)['ee-failover']
        self.assertEqual('fallback', group['type'])
        physical = {p['server'].split('.')[0]: p['name'] for p in config['proxies']}
        self.assertEqual([physical[a] for a in ['ee', 'pl', 'de']], group['proxies'])
        self.assertFalse(group['lazy'])
        self.assertEqual(30, group['interval'])
        self.assertEqual(5000, group['timeout'])
        self.assertNotIn('DIRECT', group['proxies'])
        self.assertNotIn('pl-failover', group['proxies'])
        self.assertIn('DOMAIN,canary.example.org,ee', config['rules'])

    def test_missing_primary_and_single_remaining_reserve_update_existing_config(self):
        self.rules()
        self.config(self.build())
        self.nodes(['pl'])
        config = self.config(self.build())
        groups = self.groups(config)
        self.assertEqual('fallback', groups['ee-failover']['type'])
        self.assertEqual([config['proxies'][0]['name']], groups['ee-failover']['proxies'])
        self.assertEqual(['REJECT'], groups['ee']['proxies'])

    def test_ordinary_orphan_still_preserves_last_config(self):
        self.rules(extra='  - "DOMAIN,ordinary.example.org,de"\n')
        self.config(self.build())
        before = (self.fixture.base / 'config.yaml').read_bytes()
        self.nodes(['ee', 'pl'])
        result = self.build()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, (self.fixture.base / 'config.yaml').read_bytes())

    def test_all_members_missing_never_inserts_direct(self):
        self.rules(mapping={'ee': ['pl']})
        self.config(self.build())
        before = (self.fixture.base / 'config.yaml').read_bytes()
        self.nodes(['de'])
        result = self.build()
        if result.returncode:
            self.assertEqual(before, (self.fixture.base / 'config.yaml').read_bytes())
        else:
            group = self.groups(self.config(result))['ee-failover']
            self.assertEqual(['REJECT'], group['proxies'])

    def test_malformed_metadata_is_rejected_without_changing_working_config(self):
        # Establish a legacy working configuration without relying on new functionality.
        self.fixture.rules(fixture.REAL)
        self.config(self.build())
        before = (self.fixture.base / 'config.yaml').read_bytes()
        invalid = ['[]', '{"ee":[]}', '{"ee":["pl","pl"]}', '{"ee":["ee"]}',
                   '{"ee":["unknown"]}', '{"ee":"pl"}', '{"ee":["pl"],"ee":["de"]}', '{broken']
        for value in invalid:
            with self.subTest(metadata=value):
                self.fixture.rules('cascade-fallbacks: ' + value + '\nrules:\n  - "DOMAIN-SUFFIX,example.org,de"\n')
                result = self.build()
                self.assertNotEqual(0, result.returncode, 'malformed metadata silently ignored')
                self.assertEqual(before, (self.fixture.base / 'config.yaml').read_bytes())

    def test_metadata_only_reorder_changes_config(self):
        self.rules(mapping={'ee': ['pl', 'de']})
        first = self.config(self.build())
        self.rules(mapping={'ee': ['de', 'pl']})
        second = self.config(self.build())
        self.assertNotEqual(self.groups(first)['ee-failover']['proxies'],
                            self.groups(second)['ee-failover']['proxies'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
