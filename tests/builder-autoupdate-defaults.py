#!/usr/bin/env python3
"""Full installer behavior in an unprivileged sandbox.

Only literal filesystem roots are redirected in a disposable installer copy.
Ownership flags are removed by an install shim; id/systemctl/curl are stubs.
Updater and installation control flow remain the real implementation.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class DefaultUpdaterInstallation(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(0, os.geteuid(), 'Never run installer harness as root')
        self.temp = tempfile.TemporaryDirectory(prefix='updater-defaults-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'root'
        self.source = Path(self.temp.name) / 'source'
        self.bin = Path(self.temp.name) / 'bin'
        self.bin.mkdir()
        self.root.mkdir()
        self.source.mkdir()
        for name in ('etc', 'usr'):
            shutil.copytree(REPO / name, self.source / name)
        # Do not inspect implementation to derive expectations. Redirect only
        # literal roots, retaining ${SOURCE_DIR}/... and ${dest}/... references.
        script = (REPO / 'install.sh').read_text()
        script = re.sub(r'(?<![}\w])/(?:etc|usr/local|var/lib|run)(?=[/"\s])',
                        lambda match: str(self.root) + match.group(), script)
        (self.source / 'install.sh').write_text(script)
        for name in ('etc/mihomo', 'usr/local/bin', 'usr/local/sbin', 'etc/systemd/system'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.executable(self.root / 'usr/local/bin/mihomo', 'print("Mihomo v1.19.25")\n')
        (self.root / 'etc/mihomo/geoip.metadb').write_text('synthetic database')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        DESTDIR=str(self.root), SOURCE_DIR=str(self.source),
                        MIHOMO_UPDATE_ROOT=str(self.root), TEST_ROOT=str(self.root))
        for key in ('GITHUB_REPO', 'GITHUB_REF', 'MIHOMO_INSTALL_MODE', 'INSTALL_SUBDIR'):
            self.env.pop(key, None)
        self.executable(self.bin / 'id', 'import sys\nprint("0") if "-u" in sys.argv else None\n')
        self.executable(self.bin / 'chown', 'pass\n')
        self.executable(self.bin / 'useradd', 'raise SystemExit("unexpected useradd")\n')
        real_install = shutil.which('install')
        self.executable(self.bin / 'install', '''import os,sys
args=[]; skip=False
for a in sys.argv[1:]:
    if skip: skip=False; continue
    if a in ('-o','-g','--owner','--group'): skip=True; continue
    args.append(a)
os.execv(''' + repr(real_install) + ''', ['install']+args)
''')
        self.executable(self.bin / 'curl', 'raise SystemExit("Unexpected network in full local install")\n')
        self.executable(self.bin / 'systemctl', '''import json,os,pathlib,sys
p=pathlib.Path(os.environ['TEST_ROOT']); args=sys.argv[1:]
with (p/'systemctl.log').open('a') as f: f.write(json.dumps(args)+'\\n')
cmd=next((a for a in args if not a.startswith('-')), '')
unit=args[-1] if args else ''
state=p/'update-enabled'
if cmd in ('start','restart') and unit in ('mihomo','mihomo.service'):
    if os.environ.get('TEST_START_FAIL'): sys.exit(1)
    (p/'mihomo-started').touch()
if cmd=='enable' and unit=='mihomo-update.timer':
    if not (p/'mihomo-started').exists(): sys.exit(94)
    state.touch()
if cmd=='disable' and unit=='mihomo-update.timer': state.unlink(missing_ok=True)
if cmd in ('is-enabled','is-active'):
    active=unit!='mihomo-update.timer' or state.exists()
    print(('enabled' if cmd=='is-enabled' else 'active') if active else ('disabled' if cmd=='is-enabled' else 'inactive'))
    sys.exit(0 if active else 1)
''')

    def executable(self, path, body):
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o755)

    def install(self, success=True):
        result = subprocess.run(['bash', str(self.source / 'install.sh')], env=self.env,
                                input='https://subscription.example.test/fixture\nhttps://rules.example.test/rules.yaml\n',
                                text=True, capture_output=True, timeout=25)
        if success: self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        else: self.assertNotEqual(0, result.returncode)
        return result

    def enable_calls(self):
        log = self.root / 'systemctl.log'
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [args for args in calls if 'enable' in args and 'mihomo-update.timer' in args]

    def test_fresh_canonical_enables_timer_after_service_started(self):
        self.install()
        self.assertTrue((self.root / 'mihomo-started').exists())
        self.assertEqual(1, len(self.enable_calls()))
        self.assertTrue((self.root / 'update-enabled').exists())
        self.assertTrue((self.root / 'var/lib/mihomo-updater/state.json').is_file())

    def test_existing_builder_with_missing_url_does_not_enable(self):
        (self.root / 'usr/local/sbin/mihomo-build-config').write_text('# previous installation\n')
        self.install()
        self.assertEqual([], self.enable_calls())

    def test_existing_service_alone_does_not_enable(self):
        (self.root / 'etc/systemd/system/mihomo.service').write_text('# previous service\n')
        self.install()
        self.assertEqual([], self.enable_calls())

    def test_existing_updater_state_alone_does_not_enable(self):
        state = self.root / 'var/lib/mihomo-updater/state.json'
        state.parent.mkdir(parents=True)
        state.write_text('{"schema_version":1}\n')
        self.install()
        self.assertEqual([], self.enable_calls())

    def test_existing_disabled_updater_remains_disabled_on_reinstall(self):
        self.install()
        updater = self.root / 'usr/local/sbin/mihomo-update'
        disabled = subprocess.run([sys.executable, str(updater), 'disable'], env=self.env,
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(0, disabled.returncode, disabled.stdout + disabled.stderr)
        (self.root / 'systemctl.log').write_text('')
        self.install()
        self.assertEqual([], self.enable_calls())
        self.assertFalse((self.root / 'update-enabled').exists())

    def test_custom_repo_does_not_enable_canonical_updater(self):
        self.env['GITHUB_REPO'] = 'example/custom-cascade'
        self.install()
        self.assertEqual([], self.enable_calls())

    def test_custom_ref_does_not_enable_canonical_updater(self):
        self.env['GITHUB_REF'] = 'custom-revision'
        self.install()
        self.assertEqual([], self.enable_calls())

    def test_failed_service_start_does_not_enable(self):
        self.env['TEST_START_FAIL'] = '1'
        self.install(success=False)
        calls = [json.loads(line) for line in (self.root / 'systemctl.log').read_text().splitlines()]
        self.assertTrue(any('start' in call and ('mihomo' in call or 'mihomo.service' in call) for call in calls))
        self.assertEqual([], self.enable_calls())


if __name__ == '__main__':
    unittest.main(verbosity=2)
