#!/usr/bin/env python3
"""Execute actual publisher payload through historical and current builder CLIs.

Requires PyYAML, as does cascade-failover.py. No network or root needed.
Example: python3 tests/published-rules-compat.py --payload payload.json \
    --legacy-builder /tmp/mihomo-build-config-dacb441
The caller supplies an unchanged dacb441 builder and publisher-produced flat JSON
containing rules.yaml and rules-v2.yaml. Synthetic subscription nodes are EE/PL/DE.
This proves builder compatibility, not mihomo core validation or live egress.
"""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from urllib.parse import quote

import yaml

spec = importlib.util.spec_from_file_location(
    'routing_fixture', Path(__file__).with_name('routing-rules-required.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class PublishedRulesCompat(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.RoutingRulesRequired()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        nodes = [
            'vless://11111111-2222-3333-4444-555555555555@'
            f'{alias}.example.net:443?security=tls&type=grpc&'
            f'sni={alias}.example.net&serviceName=ABC&encryption=none#'
            + quote(f'{alias.upper()} direct node')
            for alias in ('ee', 'pl', 'de')
        ]
        self.fixture.subscription.write_bytes(
            base64.b64encode(('\n'.join(nodes) + '\n').encode()))
        self.fixture.url()

    def build(self, builder, response):
        self.fixture.response.write_text(response)
        return subprocess.run(
            [sys.executable, str(builder)], env=self.fixture.env,
            cwd=self.fixture.home, capture_output=True, text=True, timeout=20)

    def successful_config(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return yaml.safe_load((self.fixture.base / 'config.yaml').read_text())

    def test_actual_published_variants(self):
        legacy = PAYLOAD['rules.yaml']
        extended = PAYLOAD['rules-v2.yaml']
        legacy_doc, extended_doc = map(yaml.safe_load, (legacy, extended))
        self.assertNotIn('cascade-fallbacks', legacy_doc)
        self.assertNotIn('cascade-probes', legacy_doc)
        fallbacks = extended_doc['cascade-fallbacks']
        self.assertEqual(fallbacks['ee'], ['pl', 'de'],
                         'proof fixture must exercise EE -> PL -> DE priority')
        self.assertTrue(any(rule.endswith(',ee-failover')
                            for rule in extended_doc['rules']))
        probes = extended_doc['cascade-probes']
        physical_probes = [rule for rule in extended_doc['rules']
                           if rule.rsplit(',', 1)[-1] in probes]
        self.assertTrue(physical_probes, 'payload must contain physical probe rules')

        # Seed a genuine previous configuration, then reproduce the old failure.
        self.successful_config(self.build(LEGACY_BUILDER, fixture.REAL))
        config_path = self.fixture.base / 'config.yaml'
        previous = config_path.read_bytes()
        rejected = self.build(LEGACY_BUILDER, extended)
        self.assertEqual(rejected.returncode, 3, rejected.stdout + rejected.stderr)
        self.assertEqual(config_path.read_bytes(), previous)

        old = self.successful_config(self.build(LEGACY_BUILDER, legacy))
        self.assertEqual(old['rules'], legacy_doc['rules'])
        self.assertFalse(any(rule.rsplit(',', 1)[-1].endswith('-failover')
                             for rule in old['rules']))
        self.assertTrue(any(rule.endswith(',ee') for rule in old['rules']))
        self.assertEqual((self.fixture.base / 'routing-rules.yaml').read_text(), legacy)

        new = self.successful_config(self.build(fixture.BUILDER, extended))
        self.assertEqual(new['rules'], extended_doc['rules'])
        self.assertEqual((self.fixture.base / 'routing-rules.yaml').read_text(), extended)
        groups = {group['name']: group for group in new['proxy-groups']}
        physical = {proxy['server'].split('.')[0]: proxy['name']
                    for proxy in new['proxies']}
        for primary, reserves in fallbacks.items():
            with self.subTest(primary=primary):
                group = groups[primary + '-failover']
                self.assertEqual(group['type'], 'fallback')
                self.assertEqual(group['proxies'],
                                 [physical[alias] for alias in [primary, *reserves]])
                self.assertNotIn('DIRECT', group['proxies'])
        for rule in physical_probes:
            self.assertIn(rule, old['rules'])
            self.assertIn(rule, new['rules'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--legacy-builder', type=Path, required=True)
    args = parser.parse_args()
    PAYLOAD = json.loads(args.payload.read_text())
    for name in ('rules.yaml', 'rules-v2.yaml'):
        if not isinstance(PAYLOAD.get(name), str):
            parser.error(f'payload must contain string {name}')
    LEGACY_BUILDER = args.legacy_builder.resolve()
    if not LEGACY_BUILDER.is_file():
        parser.error('legacy builder file does not exist')
    unittest.main(argv=[sys.argv[0]], verbosity=2)
