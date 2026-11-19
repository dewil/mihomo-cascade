#!/usr/bin/env python3
"""V3 local rules that name a policy absent from the published contract.

A local override pinned to a service that a later generation removed must not
freeze the installation on an old generation: the stale local rule is dropped
with a warning and the fresh contract is accepted. Published rules with an
unknown policy are still refused.

Run: python3 tests/v3-stale-local-rules.py (no network or root needed).
"""
import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / 'usr/local/sbin/mihomo-build-config'


def vless(host, name):
    return (f'vless://00000000-0000-4000-8000-000000000001@{host}:443'
            f'?security=tls&type=tcp#{quote(name)}')


SUB = base64.b64encode('\n'.join([vless('pl.example', 'PL pl node'),
                                  vless('us2.example', 'US2 us2 node')]).encode() + b'\n')


def published(generation, rules):
    head = ('cascade-contract: 3\n'
            f'cascade-generation: {generation}\n'
            'cascade-nodes: {"n35-pl": {"server": "pl.example"}, '
            '"n43-us2": {"server": "us2.example"}}\n'
            'cascade-services: {"svc-pl": ["n35-pl"], "svc-us2": ["n43-us2"]}\n')
    return head + 'rules:\n' + ''.join(f'  - "{r}"\n' for r in rules)


class StaleLocalRules(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-stale-local-')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / 'base'
        self.base.mkdir()
        for n in ('config.base.yaml', 'iso3166_alpha2.txt'):
            shutil.copyfile(ROOT / 'etc/mihomo' / n, self.base / n)
        sub = Path(tmp.name) / 'sub.txt'
        sub.write_bytes(SUB)
        (self.base / 'subscription.url').write_text(f'file://{sub}\n')
        (self.base / 'cascade-contract').write_text('3\n')

    def build(self, rules, local):
        (self.base / 'routing-rules.yaml').write_text(rules)
        (self.base / 'local-rules.yaml').write_text(
            'rules:\n' + ''.join(f'  - "{r}"\n' for r in local))
        env = dict(os.environ, MIHOMO_BASE=str(self.base), MIHOMO_MIN_NODES_ABS='1',
                   MIHOMO_MIN_NODES_RATIO='0.1')
        return subprocess.run([sys.executable, str(BUILDER)], env=env,
                              cwd=str(self.base.parent), capture_output=True,
                              text=True, timeout=60)

    def config(self):
        return (self.base / 'config.yaml').read_text()

    def test_stale_local_rule_dropped_contract_accepted(self):
        r = self.build(published(43, ['DOMAIN-SUFFIX,chatgpt.com,svc-us2', 'MATCH,DIRECT']),
                       ['DST-PORT,5222,REJECT', 'DOMAIN-SUFFIX,pypi.org,svc-pl',
                        'DOMAIN-SUFFIX,openai.com,svc-de3'])
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = self.config()
        self.assertIn('chatgpt.com,svc-us2', cfg)
        self.assertIn('pypi.org,svc-pl', cfg)
        self.assertIn('DST-PORT,5222,REJECT', cfg)
        self.assertNotIn('svc-de3', cfg)
        self.assertIn('svc-de3', r.stderr)
        self.assertIn('"generation": 43', (self.base / 'build-state.json').read_text())

    def test_stale_local_rule_does_not_pin_old_generation(self):
        old = published(39, ['DOMAIN-SUFFIX,chatgpt.com,svc-pl', 'MATCH,DIRECT'])
        old = old.replace('"svc-pl": ["n35-pl"]', '"svc-pl": ["n35-pl"], "svc-de3": ["n35-pl"]')
        local = ['DOMAIN-SUFFIX,openai.com,svc-de3']
        self.assertEqual(self.build(old, local).returncode, 0)
        r = self.build(published(43, ['DOMAIN-SUFFIX,chatgpt.com,svc-us2', 'MATCH,DIRECT']),
                       local)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('chatgpt.com,svc-us2', self.config())
        self.assertIn('"generation": 43', (self.base / 'build-state.json').read_text())

    def test_malformed_local_rule_still_refused(self):
        for bad in ['DOMAIN-SUFFIX,openai.com', 'DOMAIN-SUFFIX,openai.com,', 'MATCH,',
                    'IP-CIDR,10.0.0.0/8, ,no-resolve', "DOMAIN,openai.com,''"]:
            with self.subTest(rule=bad):
                r = self.build(published(43, ['DOMAIN-SUFFIX,chatgpt.com,svc-us2',
                                              'MATCH,DIRECT']), [bad])
                self.assertNotEqual(r.returncode, 0)
                self.assertIn('unknown policy', r.stderr)

    def test_published_unknown_policy_still_refused(self):
        r = self.build(published(43, ['DOMAIN-SUFFIX,chatgpt.com,svc-gone', 'MATCH,DIRECT']),
                       [])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('unknown policy', r.stderr)


if __name__ == '__main__':
    unittest.main()
