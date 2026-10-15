#!/usr/bin/env python3
"""Independent reviewer delta contracts; reuse only the blind test fixture."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

spec = importlib.util.spec_from_file_location('blind_updater', Path(__file__).with_name('builder-autoupdate.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class UpdaterReview(unittest.TestCase):
    def setUp(self):
        self.f = fixture.BuilderAutoupdate()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.write_executable(self.f.bin / 'systemctl', '''import os,pathlib,sys
p=pathlib.Path(os.environ['TEST_ROOT']); args=sys.argv[1:]
with (p/'systemctl.calls').open('a') as f: f.write(' '.join(args)+'\\n')
cmd=next((a for a in args if not a.startswith('-')), '')
state=p/'timer.enabled'
if cmd=='enable': state.touch()
if cmd=='disable': state.unlink(missing_ok=True)
if cmd==os.environ.get('TEST_QUERY_FAILURE'):
    print('Failed to connect to bus: synthetic manager error',file=sys.stderr); sys.exit(1)
if cmd in ('is-enabled','is-active'):
    if os.environ.get('TEST_UNIT_MISSING'): print('not-found');sys.exit(4)
    active=state.exists()
    print(('enabled' if active else 'disabled') if cmd=='is-enabled' else ('active' if active else 'inactive'))
    sys.exit(0 if active else (1 if cmd=='is-enabled' else 3))
''')

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(fixture.CLI), *args], env=self.f.env,
                                text=True, capture_output=True, timeout=12)
        self.assertNotIn(fixture.SECRET, result.stdout + result.stderr)
        if ok: self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        else: self.assertNotEqual(0, result.returncode, 'Unknown state incorrectly reported success: ' + result.stdout)
        return result

    def test_status_and_disable_reject_manager_query_failures(self):
        for command in ('status', 'disable'):
            for query in ('is-enabled', 'is-active'):
                with self.subTest(command=command, query=query):
                    self.f.env['TEST_QUERY_FAILURE'] = query
                    self.cli(command, ok=False)
        self.f.env.pop('TEST_QUERY_FAILURE', None)

    def test_status_and_disable_reject_missing_unit(self):
        self.f.env['TEST_UNIT_MISSING'] = '1'
        for command in ('status', 'disable'):
            with self.subTest(command=command): self.cli(command, ok=False)

    def snapshot(self):
        directory = self.f.root / 'var/lib/mihomo-updater'
        return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}

    def test_rebaseline_requires_explicit_acceptance_and_preserves_state_on_refusal(self):
        self.cli('update', '--accept-current')
        self.f.builder.write_bytes(self.f.builder.read_bytes() + b'\n# manual full-install version\n')
        before = self.snapshot()
        self.cli('rebaseline', ok=False)
        self.assertEqual(before, self.snapshot())
        for command in ('update', 'enable'):
            with self.subTest(command=command): self.cli(command, '--accept-current', ok=False)
        self.assertEqual(before, self.snapshot())

    def test_rebaseline_preserves_rejections_previous_backups_and_timer_choice(self):
        self.cli('update', '--accept-current')
        state_path = self.f.root / 'var/lib/mihomo-updater/state.json'
        state = json.loads(state_path.read_text())
        rejected = 'e' * 64
        state['rejected'].append(rejected)
        state_path.write_text(json.dumps(state))
        self.cli('disable')
        state = json.loads(state_path.read_text())
        backups = {p.name: p.read_bytes() for p in (state_path.parent / 'backups').iterdir()}
        manual = self.f.builder.read_bytes() + b'\n# manually installed revision\n'
        self.f.builder.write_bytes(manual)
        before_calls = (self.f.root / 'curl.calls').read_bytes()
        (self.f.root / 'systemctl.calls').write_text('')
        self.cli('rebaseline', '--accept-current')
        after = json.loads(state_path.read_text())
        self.assertEqual(state['rejected'], after['rejected'])
        self.assertEqual(state['previous'], after['previous'])
        self.assertEqual(backups, {p.name: p.read_bytes() for p in (state_path.parent / 'backups').iterdir()})
        self.assertEqual(before_calls, (self.f.root / 'curl.calls').read_bytes())
        self.assertEqual(manual, self.f.builder.read_bytes())
        self.assertFalse((self.f.root / 'timer.enabled').exists())
        calls = (self.f.root / 'systemctl.calls').read_text()
        self.assertFalse(any(word in calls.split() for word in ('enable','start','restart','reload')))
        self.f.assert_live_unchanged()
        self.f.candidate.write_bytes(manual)
        self.cli('update')
        self.assertEqual(manual, self.f.builder.read_bytes())

    def test_rebaseline_refuses_invalid_python_without_state_change(self):
        self.cli('update', '--accept-current')
        self.f.builder.write_text('def bad(:\n')
        before = self.snapshot()
        self.cli('rebaseline', '--accept-current', ok=False)
        self.assertEqual(before, self.snapshot())
        self.assertEqual('def bad(:\n', self.f.builder.read_text())

    def test_rebaseline_refuses_pending_transaction(self):
        self.f.pending_fixture()
        before = self.snapshot()
        builder = self.f.builder.read_bytes()
        self.cli('rebaseline', '--accept-current', ok=False)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(builder, self.f.builder.read_bytes())


if __name__ == '__main__': unittest.main(verbosity=2)
