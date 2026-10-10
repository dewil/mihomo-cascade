"""Post-review regression tests: inert adapters, no SSH, Consul or live fault."""
import ast
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev_runner as dev
import dev_fault


class DevAdapterGuards(unittest.TestCase):
    def test_network_failures_do_not_hide_internal_probe_errors(self):
        client=dev.Client.__new__(dev.Client); client.port=12345
        for code,expected in [(7,('fail','connect_failed')),(28,('fail','timeout')),(97,('fail','connect_failed')),
                              (2,('unknown','probe_error')),(60,('unknown','probe_error'))]:
            with self.subTest(code=code),patch('subprocess.run',return_value=SimpleNamespace(returncode=code,stdout=b'')):
                result=client.request('primary')
                self.assertEqual((result['state'],result['reason']),expected)
                self.assertEqual(result['curl_exit_code'],code)
        unknown=dev.typed('unknown','probe_error'); failed=dev.typed('fail','timeout'); good=dev.typed('pass','ok')
        self.assertEqual(dev.controlled_points([unknown,failed],[good,good]),[unknown,failed])
        self.assertEqual(dev.controlled_points([failed,good],[unknown,unknown]),[unknown,good])

    def test_private_runtime_requires_owned_directory_and_private_regular_config(self):
        with tempfile.TemporaryDirectory(prefix='line-probe-core-',dir='/tmp') as directory:
            path=Path(directory); cfg=path/'config.yaml'; cfg.write_text('{}'); cfg.chmod(0o600)
            expected=(path.stat().st_dev,path.stat().st_ino)
            self.assertEqual(dev.validate_client_paths(directory,str(cfg)),expected)
            cfg.chmod(0o644)
            with self.assertRaisesRegex(ValueError,'DEV_CLIENT_PATH_OWNERSHIP'):
                dev.validate_client_paths(directory,str(cfg))
            cfg.unlink(); cfg.symlink_to('/dev/null')
            with self.assertRaises(ValueError): dev.validate_client_paths(directory,str(cfg))

    def test_cleanup_failure_is_reported_instead_of_silent_success(self):
        with tempfile.TemporaryDirectory(prefix='line-probe-core-',dir='/tmp') as directory:
            client=dev.Client.__new__(dev.Client)
            client.core=SimpleNamespace(work_dir=directory,__exit__=lambda *args:None)
            client.pidfd=None; client.watchdog=None; client.owned_dir_identity=(1,2)
            with self.assertRaisesRegex(ValueError,'DEV_CLIENT_CLEANUP_FAILED'):
                client.__exit__(None,None,None)
            self.assertTrue(Path(directory).is_dir())

    def watchdog_child(self, *, fail_start=False):
        """Execute only the fork-child AST against fake OS/signal/select/run."""
        source=dev_fault.REMOTE.replace('__KIND__',repr('service_down')).replace('__UNITS__',repr(dev.UNITS)).replace('__WORKER__',repr(dev.WORKER)).replace('__VERIFIED_IPS__',repr(['192.0.2.1']))
        tree=ast.parse(source)
        branch=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and ast.unparse(n.test)=='child == 0')
        calls=[]; writes=[]
        def run(*args):
            calls.append(args)
            if fail_start and args[1]=='start': raise OSError('private data must not escape')
            return 'active'
        def stop(code): raise SystemExit(code)
        def failed_select(*args): raise OSError('interrupted wait')
        fake_os=SimpleNamespace(close=lambda *a:None,setsid=lambda:None,O_RDWR=2,
            open=lambda *a:99,dup2=lambda *a:None,write=lambda fd,data:writes.append((fd,data)),_exit=stop)
        fake_signal=SimpleNamespace(SIGHUP=1,SIG_IGN=0,signal=lambda *a:None,SIGCONT=18,
            pidfd_send_signal=lambda *a:None)
        env=dict(os=fake_os,signal=fake_signal,select=SimpleNamespace(select=failed_select),
            time=SimpleNamespace(time=lambda:100.0),json=json,run=run,KIND='service_down',
            WORKER=dev.WORKER,timeout=45,fds=[],ready_r=1,ready_w=2,done_r=3,done_w=4,restored_r=5,restored_w=6)
        with self.assertRaises(SystemExit) as stopped:
            exec(compile(ast.Module(body=branch.body,type_ignores=[]),'<inert-watchdog>','exec'),env)
        payload=json.loads(next(data for fd,data in writes if fd==6))
        return calls,payload,stopped.exception.code

    def test_worker_restore_attempts_every_fd_after_exit_or_signal_error(self):
        tree=ast.parse(dev_fault.REMOTE)
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='resume_workers')
        for first_error,expected in [(ProcessLookupError,True),(PermissionError,False)]:
            calls=[]
            def send(fd,signum):
                calls.append((fd,signum))
                if fd==10: raise first_error()
            def state(fd):
                if fd==10: raise ProcessLookupError()
                return 'R'
            env={'signal':SimpleNamespace(pidfd_send_signal=send,SIGCONT=18),
                 'worker_state':state,'time':SimpleNamespace(monotonic=lambda:0,sleep=lambda n:None)}
            exec(compile(ast.Module(body=[function],type_ignores=[]),'<inert-resume>','exec'),env)
            self.assertIs(env['resume_workers']([10,11]),expected)
            self.assertEqual(calls,[(10,18),(11,18)])

    def test_worker_restore_rejects_surviving_stopped_pid(self):
        tree=ast.parse(dev_fault.REMOTE)
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='resume_workers')
        clock=iter([0,3])
        env={'signal':SimpleNamespace(pidfd_send_signal=lambda *a:None,SIGCONT=18),
             'worker_state':lambda fd:'T','time':SimpleNamespace(monotonic=lambda:next(clock),sleep=lambda n:None)}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'<inert-resume>','exec'),env)
        self.assertFalse(env['resume_workers']([11]))

    def test_watchdog_restores_even_if_wait_raises(self):
        calls,payload,code=self.watchdog_child()
        self.assertEqual(calls,[('systemctl','start','hiddify-haproxy.service'),
                                ('systemctl','is-active','hiddify-haproxy.service')])
        self.assertTrue(payload['restored']); self.assertEqual(code,0)

    def test_failed_restore_has_negative_ack_and_cannot_report_removed(self):
        _,payload,code=self.watchdog_child(fail_start=True)
        self.assertFalse(payload['restored']); self.assertEqual(code,2)
        fault=dev_fault.DevFault('service_down',dev.UNITS,dev.WORKER,['192.0.2.1'])
        with patch.object(fault,'_line',return_value={'phase':'removed','units_active':True,'at_ms':100}):
            with self.assertRaisesRegex(ValueError,'DEV_ROLLBACK_UNVERIFIED'): fault.wait_removed()


if __name__=='__main__': unittest.main()
