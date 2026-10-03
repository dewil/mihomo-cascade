#!/usr/bin/env python3
"""CASC-RULES-REQUIRED black-box contract; no network or root needed.

Run: python3 tests/routing-rules-required.py
The builder is invoked only through its CLI and MIHOMO_BASE.
"""
import base64
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / 'usr/local/sbin/mihomo-build-config'
REAL = 'rules:\n  - "DOMAIN-SUFFIX,example.org,de"\n  - "MATCH,DIRECT"\n'
EXAMPLE = (ROOT / 'etc/mihomo/routing-rules.yaml').read_text()
REORDERED = '''# Formatting is not a real configuration
rules:
  - MATCH,DIRECT
  - 'GEOIP,RU,DIRECT' # retained comment
  - IP-CIDR,10.0.0.0/8,DIRECT
  - 'DOMAIN-SUFFIX,whatismyipaddress.com,de'
'''
INVALID = {'missing': None, 'empty': '', 'empty_list': 'rules: []\n',
           'direct_only': 'rules:\n  - MATCH,DIRECT\n',
           'example': EXAMPLE, 'reordered_example': REORDERED}


class RoutingRulesRequired(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='routing-required-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.base = self.home / 'base'
        self.base.mkdir()
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        for name in ('config.base.yaml', 'iso3166_alpha2.txt'):
            shutil.copyfile(ROOT / 'etc/mihomo' / name, self.base / name)
        (self.base / 'local-rules.yaml').write_text('rules: []\n')
        (self.base / 'subscription.url').write_text('https://subscription.example.invalid/nodes\n')
        nodes = []
        for country, flag in [('de', '🇩🇪'), ('ee', '🇪🇪')]:
            nodes.append('vless://11111111-2222-3333-4444-555555555555@'
                         f'{country}.example.net:443?security=tls&type=grpc&'
                         f'sni={country}.example.net&serviceName=ABC&encryption=none#'
                         + quote(f'{flag} {country.upper()} direct node'))
        self.subscription = self.home / 'subscription.txt'
        self.subscription.write_bytes(base64.b64encode(('\n'.join(nodes) + '\n').encode()))
        self.response = self.home / 'rules-response.yaml'
        self.response.write_text(REAL)
        self.calls = self.home / 'curl-calls.txt'
        # Record request category only: URLs (potentially secret-bearing) stay out of logs.
        curl = self.bin / 'curl'
        curl.write_text('#!' + sys.executable + '\n' + '''import os, pathlib, sys
args = sys.argv[1:]
url = next((x for x in args if x.startswith(('https://', 'http://', 'file://'))), '')
is_sub = url.startswith('https://subscription.example.invalid/')
with open(os.environ['TEST_CURL_CALLS'], 'a') as log:
    log.write('subscription\\n' if is_sub else 'rules\\n')
if not is_sub and os.environ.get('TEST_RULES_NETWORK_FAIL') == '1':
    sys.exit(22)
data = pathlib.Path(os.environ['TEST_SUBSCRIPTION'] if is_sub else os.environ['TEST_RULES_RESPONSE']).read_bytes()
output = None
for i, arg in enumerate(args):
    if arg in ('-o', '--output'):
        output = args[i + 1]
    elif arg.startswith('--output='):
        output = arg.split('=', 1)[1]
if output:
    pathlib.Path(output).write_bytes(data)
else:
    sys.stdout.buffer.write(data)
''')
        curl.chmod(0o755)
        for name in ('chown', 'chmod'):
            command = self.bin / name
            command.write_text('#!/bin/sh\nexit 0\n')
            command.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''),
                        MIHOMO_BASE=str(self.base), MIHOMO_MIN_NODES_ABS='1',
                        MIHOMO_MIN_NODES_RATIO='0.1', TEST_SUBSCRIPTION=str(self.subscription),
                        TEST_RULES_RESPONSE=str(self.response), TEST_CURL_CALLS=str(self.calls))
        self.env.pop('MIHOMO_ALLOW_SHRINK', None)

    def rules(self, content):
        path = self.base / 'routing-rules.yaml'
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(content)

    def url(self, value='https://rules.example.invalid/routing.yaml?token=SYNTHETIC_URL_MARKER'):
        (self.base / 'routing-rules.url').write_text(value + '\n')

    def build(self, **extra):
        return subprocess.run([sys.executable, str(BUILDER)], env=dict(self.env, **extra),
                              cwd=self.home, capture_output=True, text=True, timeout=20)

    def snapshot(self):
        # Whole sandbox includes public outputs config.yaml, aliases.yaml and
        # build-state.json, as well as mandatory inputs that must stay atomic.
        return {p.relative_to(self.base).as_posix(): p.read_bytes()
                for p in self.base.rglob('*') if p.is_file()}

    def reject(self, result, before):
        for name in ('config.yaml', 'aliases.yaml', 'build-state.json'):
            path = self.base / name
            actual = path.read_bytes() if path.exists() else None
            self.assertEqual(actual, before.get(name),
                             f'rejected build created or modified {name}')
        self.assertNotEqual(result.returncode, 0, 'builder accepted unusable mandatory rules')
        self.assertEqual(self.snapshot(), before, 'rejected build modified config/state/aliases or inputs')
        self.assertRegex(result.stdout + result.stderr,
                         r'(?i)(правил|rules|настройк|configurat)',
                         'failure must explain missing real rules or unconfigured URL')

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = (self.base / 'config.yaml').read_text()
        self.assertIn('DOMAIN-SUFFIX,example.org,de', config)
        self.assertTrue((self.base / 'build-state.json').is_file())

    def test_01_invalid_local_inputs_fresh_and_existing(self):
        for name, content in INVALID.items():
            for existing in (False, True):
                with self.subTest(input=name, existing=existing):
                    # Independent sandbox per case prevents artifacts from disguising failures.
                    self.setUp()
                    if existing:
                        self.rules(REAL)
                        self.success(self.build())
                    self.rules(content)
                    before = self.snapshot()
                    self.reject(self.build(), before)

    def test_02_placeholder_host_rejected_without_download_or_url_leak(self):
        for content in (None, REAL):
            with self.subTest(real_local=content is not None):
                self.setUp()
                self.rules(content)
                url = 'https://subscribe_domain/private?token=SYNTHETIC_NOT_A_SECRET'
                self.url(url)
                before = self.snapshot()
                result = self.build()
                self.assertNotIn(url, result.stdout + result.stderr)
                self.assertNotIn('SYNTHETIC_NOT_A_SECRET', result.stdout + result.stderr)
                self.reject(result, before)
                calls = self.calls.read_text() if self.calls.exists() else ''
                self.assertNotIn('rules', calls, 'placeholder host must be refused before rule download')

    def test_02_similar_real_hostname_is_allowed(self):
        self.rules(None)
        self.url('https://real-subscribe_domain.example.invalid/routing.yaml')
        self.success(self.build())

    def test_03_real_local_without_url(self):
        for url in (None, ''):
            with self.subTest(url=url):
                self.setUp()
                self.rules(REAL)
                if url is not None:
                    self.url(url)
                self.success(self.build())

    def test_04_network_failure_with_real_rules_warns(self):
        self.rules(REAL)
        self.url()
        self.success(result := self.build(TEST_RULES_NETWORK_FAIL='1'))
        self.assertRegex(result.stdout + result.stderr, r'(?i)(предупрежден|warn)')
        self.assertNotIn('SYNTHETIC_URL_MARKER', result.stdout + result.stderr)
        self.assertEqual((self.base / 'routing-rules.yaml').read_text(), REAL)

    def test_04_network_failure_without_real_rules_rejected(self):
        for name in ('missing', 'example'):
            with self.subTest(input=name):
                self.setUp()
                self.rules(INVALID[name])
                self.url()
                before = self.snapshot()
                self.reject(self.build(TEST_RULES_NETWORK_FAIL='1'), before)

    def test_05_invalid_download_preserves_real_previous_rules_and_warns(self):
        for name, response in INVALID.items():
            if response is None:
                continue
            with self.subTest(response=name):
                self.setUp()
                self.rules(REAL)
                self.url()
                self.response.write_text(response)
                self.success(result := self.build())
                self.assertEqual((self.base / 'routing-rules.yaml').read_text(), REAL)
                self.assertRegex(result.stdout + result.stderr, r'(?i)(предупрежден|warn)')
                self.assertNotIn('SYNTHETIC_URL_MARKER', result.stdout + result.stderr)

    def test_05_invalid_download_without_real_previous_rejected(self):
        for name, response in INVALID.items():
            if response is None:
                continue
            for previous in (None, EXAMPLE):
                with self.subTest(response=name, existing_example=previous is not None):
                    self.setUp()
                    self.rules(previous)
                    self.url()
                    self.response.write_text(response)
                    before = self.snapshot()
                    self.reject(self.build(), before)

    def test_06_real_download_replaces_example(self):
        self.rules(EXAMPLE)
        self.url()
        self.success(self.build())
        self.assertEqual((self.base / 'routing-rules.yaml').read_text(), REAL)

    def test_07_local_rules_and_allow_shrink_cannot_bypass(self):
        for name, content in INVALID.items():
            with self.subTest(input=name):
                self.setUp()
                self.rules(content)
                (self.base / 'local-rules.yaml').write_text(REAL)
                before = self.snapshot()
                self.reject(self.build(MIHOMO_ALLOW_SHRINK='1'), before)

    def test_01_example_with_unrelated_top_level_list_is_rejected(self):
        # A list belonging to another YAML section is not a routing rule.
        content = EXAMPLE + '\nproxy-groups:\n  - name: extra\n'
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.setUp()
                if existing:
                    self.rules(REAL)
                    self.success(self.build())
                self.rules(content)
                before = self.snapshot()
                self.reject(self.build(), before)

    def test_05_example_download_with_unrelated_top_level_list_preserves_real_rules(self):
        self.rules(REAL)
        self.url()
        self.response.write_text(EXAMPLE + '\nproxy-groups:\n  - name: extra\n')
        result = self.build()
        self.assertEqual((self.base / 'routing-rules.yaml').read_text(), REAL,
                         'unrelated YAML list made the example replace real routing rules')
        self.success(result)
        self.assertRegex(result.stdout + result.stderr, r'(?i)(предупрежден|warn)')
        self.assertNotIn('SYNTHETIC_URL_MARKER', result.stdout + result.stderr)

    def test_08_service_requires_builder_success(self):
        unit = (ROOT / 'etc/systemd/system/mihomo.service').read_text()
        commands = re.findall(r'^ExecStartPre=(.*)$', unit, re.MULTILINE)
        builder_commands = [cmd for cmd in commands if 'mihomo-build-config' in cmd]
        self.assertTrue(builder_commands, 'service must run builder before startup')
        for command in builder_commands:
            self.assertNotIn('-', command.split('/')[0], 'systemd must not ignore builder exit status')


if __name__ == '__main__':
    unittest.main(verbosity=2)
