#!/usr/bin/env python3
"""CASC-AUTOUPDATE black-box contracts; no live network or systemd calls."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / 'usr/local/sbin/mihomo-update'
SHA = 'a' * 40
SECRET = 'SYNTHETIC_PRIVATE_SUBSCRIPTION_MARKER'


class BuilderAutoupdate(unittest.TestCase):
    def setUp(self):
        self.assertTrue(CLI.is_file(), 'Public updater CLI has not been implemented')
        self.tmp = tempfile.TemporaryDirectory(prefix='builder-update-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / 'etc/mihomo'
        self.base.mkdir(parents=True)
        self.bin = self.root / 'test-bin'
        self.bin.mkdir()
        self.builder = self.root / 'usr/local/sbin/mihomo-build-config'
        self.builder.parent.mkdir(parents=True)
        self.old = b'#!/usr/bin/env python3\n# original builder\n'
        self.builder.write_bytes(self.old)
        self.builder.chmod(0o751)
        self.core = self.root / 'usr/local/bin/mihomo'
        self.core.parent.mkdir(parents=True)
        self.write_executable(self.core, '''import os, pathlib, sys
p=pathlib.Path(os.environ['TEST_ROOT'])
with (p/'core.calls').open('a') as f: f.write(' '.join(sys.argv[1:])+'\\n')
print(os.environ['TEST_SECRET'], file=sys.stderr)
sys.exit(int(os.environ.get('TEST_CORE_EXIT','0')))
''')
        for name, value in {'config.yaml': 'rules: [MATCH,DIRECT]\n',
                            'build-state.json': '{"previous_nodes":7}\n',
                            'subscription.url': 'https://subscription.example.test/' + SECRET,
                            'rules.local.yaml': 'rules: [MATCH,REJECT]\n'}.items():
            (self.base / name).write_text(value)
        self.live = {p.name: p.read_bytes() for p in self.base.iterdir()}
        self.candidate = self.root / 'candidate.py'
        self.good_candidate()
        self.metadata = self.root / 'metadata.json'
        self.metadata.write_text(json.dumps({'sha': SHA, 'download_url': 'https://evil.invalid/ignored'}))
        self.env = dict(os.environ, MIHOMO_UPDATE_ROOT=str(self.root), TEST_ROOT=str(self.root),
                        TEST_SECRET=SECRET, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        MIHOMO_ALLOW_SHRINK='1', MIHOMO_UPDATE_LOCK_TIMEOUT='1',
                        MIHOMO_UPDATE_COMMAND_TIMEOUT='1', MIHOMO_UPDATE_FETCH_TIMEOUT='1')
        self.write_executable(self.bin / 'curl', '''import json, os, pathlib, sys
p=pathlib.Path(os.environ['TEST_ROOT']); args=sys.argv[1:]
urls=[x for x in args if x.startswith('https://')]
if len(urls)!=1: sys.exit(91)
u=urls[0]
with (p/'curl.calls').open('a') as f: f.write(json.dumps(args)+'\\n')
if u=='https://api.github.com/repos/dewil/mihomo-cascade/commits/main': body=(p/'metadata.json').read_bytes()
elif u.startswith('https://raw.githubusercontent.com/dewil/mihomo-cascade/') and u.endswith('/usr/local/sbin/mihomo-build-config'):
    sha=u.split('/')[5]
    if len(sha)!=40 or any(c not in '0123456789abcdefABCDEF' for c in sha): sys.exit(92)
    body=(p/'candidate.py').read_bytes()
else: sys.exit(93)
if os.environ.get('TEST_NETWORK_FAIL'): sys.exit(22)
output=None
for i,a in enumerate(args):
    if a in ('-o','--output'): output=args[i+1]
    elif a.startswith('--output='): output=a.split('=',1)[1]
if output: pathlib.Path(output).write_bytes(body)
else: sys.stdout.buffer.write(body)
''')
        self.write_executable(self.bin / 'systemctl', '''import os, pathlib, sys
p=pathlib.Path(os.environ['TEST_ROOT']); args=sys.argv[1:]
with (p/'systemctl.calls').open('a') as f: f.write(' '.join(args)+'\\n')
cmd=next((a for a in args if not a.startswith('-')), '')
state=p/'timer.enabled'
if cmd=='enable': state.touch()
elif cmd=='disable': state.unlink(missing_ok=True)
elif cmd=='is-enabled': print('enabled' if state.exists() else 'disabled'); sys.exit(0 if state.exists() else 1)
elif cmd=='is-active': print('active' if state.exists() else 'inactive'); sys.exit(0 if state.exists() else 3)
''')

    def write_executable(self, path, body):
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o755)

    def good_candidate(self, exit_code=0):
        self.candidate.write_text('''#!/usr/bin/env python3
import os, pathlib, sys
root=pathlib.Path(os.environ['TEST_ROOT'])
base=pathlib.Path(os.environ['MIHOMO_BASE'])
assert base != root/'etc/mihomo'
assert 'MIHOMO_ALLOW_SHRINK' not in os.environ
assert (base/'config.yaml').read_bytes()==(root/'etc/mihomo/config.yaml').read_bytes()
assert (base/'build-state.json').read_bytes()==(root/'etc/mihomo/build-state.json').read_bytes()
(root/'candidate.executed').touch()
(base/'config.yaml').write_text('rules: [MATCH,REJECT]\\n')
print(os.environ['TEST_SECRET']); print(os.environ['TEST_SECRET'], file=sys.stderr)
sys.exit(''' + str(exit_code) + ')\n')

    def run_cli(self, command, ok=True, accept_current=False):
        args = [sys.executable, str(CLI), command] + (['--accept-current'] if accept_current else [])
        result = subprocess.run(args, env=self.env,
                                text=True, capture_output=True, timeout=35)
        self.assertNotIn(SECRET, result.stdout + result.stderr, 'Raw subprocess secrets leaked')
        if ok:
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        else:
            self.assertNotEqual(0, result.returncode, 'Rejected update reported success')
        return result

    def assert_live_unchanged(self):
        self.assertEqual(self.live, {p.name: p.read_bytes() for p in self.base.iterdir() if p.is_file()})
        log = self.root / 'systemctl.calls'
        if log.exists():
            for line in log.read_text().splitlines():
                self.assertFalse(any(word in line.split() for word in ('restart', 'reload', 'reboot')), line)
                self.assertNotIn('mihomo-refresh', line)

    def test_check_is_nonexecuting_and_uses_immutable_source(self):
        result = self.run_cli('check')
        self.assertFalse((self.root / 'candidate.executed').exists())
        self.assertEqual(self.old, self.builder.read_bytes())
        self.assert_live_unchanged()
        self.assertIn(SHA, result.stdout + result.stderr)
        calls = (self.root / 'curl.calls').read_text()
        self.assertIn('/' + SHA + '/usr/local/sbin/mihomo-build-config', calls)
        self.assertNotIn('evil.invalid', calls)

    def test_success_validates_staged_config_and_changes_only_builder(self):
        self.run_cli('update', accept_current=True)
        self.assertEqual(self.candidate.read_bytes(), self.builder.read_bytes())
        self.assertTrue((self.root / 'candidate.executed').exists())
        self.assertIn('-t', (self.root / 'core.calls').read_text().split())
        self.assert_live_unchanged()
        state = self.root / 'var/lib/mihomo-updater'
        self.assertEqual(0o700, state.stat().st_mode & 0o777)
        backups = [p for p in state.rglob('*') if p.is_file() and p.read_bytes() == self.old]
        self.assertTrue(backups, 'Previous builder bytes must be backed up')
        for p in state.rglob('*'):
            if p.is_file(): self.assertEqual(0, p.stat().st_mode & 0o077, str(p))
        status = self.run_cli('status')
        self.assertIn(hashlib.sha256(self.builder.read_bytes()).hexdigest(), status.stdout + status.stderr)

    def test_noop_retains_backup_and_rollback_rejects_same_bytes_at_new_commit(self):
        self.run_cli('update', accept_current=True)
        self.run_cli('update', accept_current=True)
        self.run_cli('rollback')
        self.assertEqual(self.old, self.builder.read_bytes())
        self.assertEqual(0o751, self.builder.stat().st_mode & 0o777)
        self.metadata.write_text(json.dumps({'sha': 'b' * 40}))
        # A blocked hash may be a reported no-op or a failure; neither may install it.
        result = subprocess.run([sys.executable, str(CLI), 'update', '--accept-current'], env=self.env,
                                capture_output=True, text=True, timeout=35)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        self.assertEqual(self.old, self.builder.read_bytes())
        self.assert_live_unchanged()

    def test_local_edit_after_success_is_not_overwritten(self):
        self.run_cli('update', accept_current=True)
        local = self.builder.read_bytes() + b'\n# local operator change\n'
        self.builder.write_bytes(local)
        self.candidate.write_bytes(self.candidate.read_bytes() + b'\n# upstream revision\n')
        self.metadata.write_text(json.dumps({'sha': 'b' * 40}))
        self.run_cli('update', ok=False, accept_current=True)
        self.assertEqual(local, self.builder.read_bytes())

    def test_bad_candidates_and_validation_failures_preserve_installation(self):
        for case in ('sha', 'json', 'empty', 'syntax', 'exit1', 'exit3', 'core', 'missing-core', 'network'):
            with self.subTest(case=case):
                self.metadata.write_text(json.dumps({'sha': SHA}))
                self.good_candidate()
                self.env.pop('TEST_CORE_EXIT', None)
                self.env.pop('TEST_NETWORK_FAIL', None)
                if case == 'sha': self.metadata.write_text(json.dumps({'sha': '../main'}))
                if case == 'json': self.metadata.write_text('{bad-json')
                if case == 'empty': self.candidate.write_bytes(b'')
                if case == 'syntax': self.candidate.write_text('def broken(:\n')
                if case in ('exit1', 'exit3'): self.good_candidate(int(case[-1]))
                if case == 'core': self.env['TEST_CORE_EXIT'] = '1'
                if case == 'network': self.env['TEST_NETWORK_FAIL'] = '1'
                if case == 'missing-core': self.core.rename(self.core.with_suffix('.saved'))
                try:
                    self.run_cli('update', ok=False, accept_current=True)
                    self.assertEqual(self.old, self.builder.read_bytes())
                    self.assert_live_unchanged()
                finally:
                    if case == 'missing-core': self.core.with_suffix('.saved').rename(self.core)

    def test_untracked_install_requires_explicit_acceptance(self):
        for command in ('enable', 'update'):
            result = self.run_cli(command, ok=False)
            self.assertIn('--accept-current', result.stdout + result.stderr)
            self.assertEqual(self.old, self.builder.read_bytes())
            self.assertFalse((self.root / 'candidate.executed').exists())
        self.run_cli('update', accept_current=True)
        self.assertEqual(self.candidate.read_bytes(), self.builder.read_bytes())
        self.assertFalse((self.root / 'timer.enabled').exists())

    def test_core_receives_staging_directory_and_config(self):
        self.run_cli('update', accept_current=True)
        import shlex
        args = shlex.split((self.root / 'core.calls').read_text().strip())
        self.assertIn('-d', args)
        self.assertIn('-f', args)
        directory = Path(args[args.index('-d') + 1])
        config = Path(args[args.index('-f') + 1])
        self.assertEqual(directory / 'config.yaml', config)
        self.assertNotEqual(self.base, directory)

    def test_generated_absolute_and_traversing_provider_paths_are_rejected(self):
        for setting in ('proxy-providers: {x: {type: file, path: /tmp/updater-outside}}\n',
                        'proxy-providers:\n  x:\n    type: file\n    path: ../outside\n'):
            with self.subTest(setting=setting):
                self.candidate.write_text('import os,pathlib\n'
                    + 'p=pathlib.Path(os.environ["MIHOMO_BASE"])/"config.yaml"\n'
                    + 'p.write_text(' + repr(setting) + ')\n')
                self.run_cli('update', ok=False, accept_current=True)
                self.assertEqual(self.old, self.builder.read_bytes())
                self.assertFalse((self.root / 'core.calls').exists(), 'Unsafe config must not reach core')
                self.assert_live_unchanged()

    def test_timeout_terminates_candidate_descendants(self):
        self.candidate.write_text("""import os,pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
(pathlib.Path(os.environ['TEST_ROOT'])/'child.pid').write_text(str(child.pid))
time.sleep(30)
""")
        self.run_cli('update', ok=False, accept_current=True)
        pid = int((self.root / 'child.pid').read_text())
        def alive():
            status = Path('/proc') / str(pid) / 'stat'
            return status.exists() and status.read_text().split()[2] != 'Z'
        try:
            limit = time.monotonic() + 2
            while alive() and time.monotonic() < limit: time.sleep(0.05)
            self.assertFalse(alive(), 'Timed-out candidate child survived lock release')
        finally:
            if alive(): os.kill(pid, 9)
        self.assertEqual(self.old, self.builder.read_bytes())

    def test_candidate_timeout_preserves_installation(self):
        self.candidate.write_text('import time\ntime.sleep(20)\n')
        started = time.monotonic()
        self.run_cli('update', ok=False, accept_current=True)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(self.old, self.builder.read_bytes())
        self.assert_live_unchanged()

    def pending_fixture(self, corrupt=False):
        state_dir = self.root / 'var/lib/mihomo-updater'
        (state_dir / 'backups').mkdir(parents=True, mode=0o700)
        state_dir.chmod(0o700)
        def metadata(data, commit):
            return dict(sha256=hashlib.sha256(data).hexdigest(), commit=commit,
                        mode=0o751, uid=os.getuid(), gid=os.getgid())
        before = metadata(self.old, None)
        after = metadata(self.candidate.read_bytes(), SHA)
        backup = state_dir / 'backups' / (before['sha256'] + '.py')
        backup.write_bytes(b'tampered backup' if corrupt else self.old)
        backup.chmod(0o600)
        state = dict(schema_version=1, installed=before, previous=None, rejected=[],
                     pending=dict(before=before, after=after))
        state_file = state_dir / 'state.json'
        state_file.write_text(json.dumps(state))
        state_file.chmod(0o600)
        self.builder.write_bytes(self.candidate.read_bytes())
        return state_file, after

    def test_pending_transaction_restores_verified_backup_without_new_update(self):
        state_file, after = self.pending_fixture()
        result = self.run_cli('update')
        self.assertIn('recover', (result.stdout + result.stderr).lower())
        self.assertEqual(self.old, self.builder.read_bytes())
        state = json.loads(state_file.read_text())
        self.assertIsNone(state['pending'])
        self.assertIn(after['sha256'], state['rejected'])
        self.assertFalse((self.root / 'candidate.executed').exists())
        self.assert_live_unchanged()

    def test_bad_backup_hash_blocks_recovery(self):
        self.pending_fixture(corrupt=True)
        installed = self.builder.read_bytes()
        self.run_cli('rollback', ok=False)
        self.assertEqual(installed, self.builder.read_bytes())
        self.assert_live_unchanged()

    def test_enable_disable_only_affect_update_timer(self):
        self.run_cli('enable', accept_current=True)
        self.run_cli('status')
        self.run_cli('disable')
        status = self.run_cli('status')
        self.assertIn('disabled', status.stdout + status.stderr)
        calls = (self.root / 'systemctl.calls').read_text()
        self.assertIn('mihomo-update.timer', calls)
        self.assertNotIn('mihomo.service', calls)
        self.assert_live_unchanged()
        self.assertEqual(self.old, self.builder.read_bytes())

    def test_refresh_lock_prevents_concurrent_mutation(self):
        path = self.root / 'run/mihomo-refresh.lock'
        path.parent.mkdir()
        with path.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            proc = subprocess.Popen([sys.executable, str(CLI), 'update', '--accept-current'], env=self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(0.4)
                self.assertEqual(self.old, self.builder.read_bytes())
                self.assertFalse((self.root / 'candidate.executed').exists())
                fcntl.flock(lock, fcntl.LOCK_UN)
                stdout, stderr = proc.communicate(timeout=35)
                self.assertNotIn(SECRET, stdout + stderr)
            finally:
                if proc.poll() is None: proc.kill(); proc.wait()


if __name__ == '__main__':
    unittest.main(verbosity=2)
