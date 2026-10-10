#!/usr/bin/env python3
"""LLM-LOCAL-FALLBACK L01–03: blind public CLI tests, synthetic inputs only.

Oracle: accepted docs/dev/2026-10-10-spec-llm-local-fallback.md.
Target faults: ignored local policy, invalid-input fail-open, orphan bypass.
No generator imports or implementation inspection; curl is fixture-local.
"""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'cascade_fixture', Path(__file__).with_name('cascade-failover.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class LocalCascadeFallbacks(unittest.TestCase):
    def setUp(self):
        self.case = fixture.CascadeFailover()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.f = self.case.fixture
        self.f.env['MIHOMO_ETC'] = str(self.f.base)
        self.local = self.f.base / 'cascade-fallbacks.local.json'
        self.case.rules()
        (self.f.base / 'local-rules.yaml').write_text(
            'rules:\n  - "DOMAIN,openai.example.org,ee-failover"\n'
            '  - "DOMAIN,ee-canary.example.org,ee"\n')

    def build(self):
        return self.case.config(self.case.build())

    def members(self, config, alias='ee'):
        return self.case.groups(config)[alias + '-failover']['proxies']

    def physical(self, config, aliases):
        names = {p['server'].split('.')[0]: p['name'] for p in config['proxies']}
        return [names[a] for a in aliases]

    def override(self, mapping):
        self.local.write_text(json.dumps(mapping))

    def outputs(self):
        return {name: (self.f.base / name).read_bytes()
                for name in ('config.yaml', 'aliases.yaml', 'build-state.json')
                if (self.f.base / name).exists()}

    def rejected(self, before):
        result = self.case.build()
        self.assertEqual(3, result.returncode, result.stdout + result.stderr)
        self.assertEqual(before, self.outputs(), 'rejection modified previous outputs')

    def test_L01_override_preserves_other_groups_probes_and_physical_canary(self):
        central = self.build()
        self.override({'ee': ['de', 'pl']})
        actual = self.build()
        self.assertEqual(self.physical(actual, ['ee', 'de', 'pl']), self.members(actual))
        for name, group in self.case.groups(central).items():
            if name != 'ee-failover':
                self.assertEqual(group, self.case.groups(actual)[name], name)
        expected = dict(self.case.groups(central)['ee-failover'])
        expected['proxies'] = self.physical(actual, ['ee', 'de', 'pl'])
        self.assertEqual(expected, self.case.groups(actual)['ee-failover'])
        self.assertIn('DOMAIN,openai.example.org,ee-failover', actual['rules'])
        self.assertIn('DOMAIN,ee-canary.example.org,ee', actual['rules'])
        self.assertEqual(central['rules'], actual['rules'])
        self.assertEqual(central['proxies'], actual['proxies'])

    def test_L01_missing_primary_updates_previous_config_in_de_pl_order(self):
        self.override({'ee': ['de', 'pl']})
        self.build()
        self.case.nodes(['de', 'pl'])
        actual = self.build()
        self.assertEqual(self.physical(actual, ['de', 'pl']), self.members(actual))
        self.assertEqual(['REJECT'], self.case.groups(actual)['ee']['proxies'])
        self.assertNotIn('DIRECT', self.members(actual))

    def test_L02_absence_byteexact_reorder_and_delete_restore_central(self):
        self.build()
        original = (self.f.base / 'config.yaml').read_bytes()
        self.build()
        self.assertEqual(original, (self.f.base / 'config.yaml').read_bytes())
        self.override({'ee': ['de', 'pl']})
        actual = self.build()
        changed = (self.f.base / 'config.yaml').read_bytes()
        self.assertNotEqual(original, changed)
        self.assertEqual(self.physical(actual, ['ee', 'de', 'pl']), self.members(actual))
        self.override({'ee': ['pl', 'de']})
        self.build()
        self.assertEqual(original, (self.f.base / 'config.yaml').read_bytes())
        self.override({'ee': ['de', 'pl']})
        self.build()
        self.local.unlink()
        self.build()
        self.assertEqual(original, (self.f.base / 'config.yaml').read_bytes())

    def test_L02_invalid_local_preserves_config(self):
        self.build()
        before = self.outputs()
        invalid = ['', '{broken', 'null', '[]', '{}', '{"ee":[]}',
                   '{"ee":"de"}', '{"ee":[1]}', '{"ee":[null]}',
                   '{"ee":["ee"]}', '{"ee":["de","de"]}',
                   '{"ee":["unknown"]}', '{"unknown":["de"]}',
                   '{"ee":["DIRECT"]}', '{"ee":["REJECT"]}',
                   '{"ee":["pl-failover"]}', '{"ee-failover":["de"]}',
                   '{"ee":["de"],"ee":["pl"]}']
        for raw in invalid:
            with self.subTest(local=raw):
                self.local.write_text(raw)
                self.rejected(before)

    def test_L02_unreadable_local_preserves_config_even_when_running_as_root(self):
        self.build()
        before = self.outputs()
        # A directory cannot be read as JSON even under root; chmod(000) can.
        self.local.mkdir()
        self.rejected(before)

    def test_L02_valid_override_cannot_hide_invalid_central_mapping(self):
        self.build()
        before = self.outputs()
        self.override({'ee': ['de', 'pl']})
        invalid = ['{"ee":[]}', '{"ee":["ee"]}', '{"ee":["unknown"]}',
                   '{"ee":["pl","pl"]}', '{"ee":"pl"}',
                   '{"ee":["pl"],"ee":["de"]}']
        for raw in invalid:
            with self.subTest(central=raw):
                self.f.rules('cascade-fallbacks: ' + raw + '\n'
                             'rules:\n  - "DOMAIN,example.org,ee-failover"\n')
                self.rejected(before)

    def test_L03_node_floor_and_required_rules_not_bypassed(self):
        self.override({'ee': ['de', 'pl']})
        self.build()
        before = self.outputs()
        with self.subTest(guard='node-floor'):
            self.f.env['MIHOMO_MIN_NODES_ABS'] = '3'
            self.case.nodes(['de', 'pl'])
            result = self.case.build()
            self.assertNotEqual(0, result.returncode)
            self.assertEqual(before, self.outputs())
        self.f.env['MIHOMO_MIN_NODES_ABS'] = '1'
        self.case.nodes(['ee', 'de', 'pl'])
        with self.subTest(guard='required-rules'):
            self.f.rules('rules: []\n')
            result = self.case.build()
            self.assertNotEqual(0, result.returncode)
            self.assertEqual(before, self.outputs())

    def test_L03_ordinary_orphan_guard_with_valid_override(self):
        self.override({'ee': ['de', 'pl']})
        self.case.rules(extra='  - "DOMAIN,ordinary.example.org,de"\n')
        self.build()
        before = self.outputs()
        self.case.nodes(['ee', 'pl'])
        result = self.case.build()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, self.outputs())


if __name__ == '__main__':
    unittest.main(verbosity=2)
