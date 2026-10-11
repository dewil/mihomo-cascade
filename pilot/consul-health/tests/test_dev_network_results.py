"""Independent post-fix regressions for the public DEV probe result contract.

Written without reading dev_runner implementation. The fix already existed when
these tests were written: no pre-fix RED phase is claimed. All subprocess calls
are mocked; no Core construction, SSH, network request or fault is executed.
"""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev_runner


class DevNetworkResultContract(unittest.TestCase):
    def setUp(self):
        self.client = dev_runner.Client.__new__(dev_runner.Client)
        self.client.port = 34000

    def assert_result(self, result, state, reason):
        self.assertEqual(result['state'], state)
        self.assertEqual(result['reason'], reason)

    def request_result(self, endpoint, *, code=0, stdout=b'', **options):
        completed = subprocess.CompletedProcess(args=['curl'], returncode=code,
                                                stdout=stdout, stderr=b'')
        with patch.object(dev_runner.subprocess, 'run', return_value=completed) as run:
            result = self.client.request(endpoint, **options)
        run.assert_called_once()
        return result

    def test_curl_connection_failure_is_a_failed_measurement(self):
        self.assert_result(self.request_result('primary', code=7), 'fail', 'connect_failed')

    def test_curl_socks_upstream_failure_is_a_failed_measurement(self):
        # SOCKS can wrap refused upstream connections as curl exit 97.
        # Independent RED observed before the fix: state unknown, expected fail;
        # the other 10 network-result regressions remained green.
        self.assert_result(self.request_result('primary', code=97), 'fail', 'connect_failed')

    def test_curl_timeout_is_a_failed_measurement(self):
        self.assert_result(self.request_result('primary', code=28), 'fail', 'timeout')

    def test_malformed_curl_output_is_unknown_not_network_failure(self):
        for stdout in [b'not an HTTP result', b'body\nnot-a-status']:
            with self.subTest(stdout=stdout):
                self.assert_result(self.request_result('primary', stdout=stdout),
                                   'unknown', 'probe_error')

    def test_internal_subprocess_exception_is_unknown(self):
        with patch.object(dev_runner.subprocess, 'run', side_effect=OSError('synthetic probe error')):
            self.assert_result(self.client.request('primary'), 'unknown', 'probe_error')

    def test_primary_204_is_success(self):
        self.assert_result(self.request_result('primary', stdout=b'\n204'), 'pass', 'ok')

    def test_secondary_200_with_ip_body_is_success(self):
        self.assert_result(self.request_result('secondary', stdout=b'ip=192.0.2.42\n\n200'),
                           'pass', 'ok')

    def test_head_http_503_is_reachable_per_mihomo_contract(self):
        self.assert_result(self.request_result('primary', head=True, stdout=b'\n503'),
                           'pass', 'ok')

    def test_successful_control_does_not_promote_unknown_measurement(self):
        unknown = {'state': 'unknown', 'reason': 'probe_error'}
        good = {'state': 'pass', 'reason': 'ok'}
        self.assertEqual(dev_runner.controlled_points([unknown], [good]), [unknown])

    def test_failed_control_invalidates_failed_measurement(self):
        failure = {'state': 'fail', 'reason': 'connect_failed'}
        control = {'state': 'fail', 'reason': 'timeout'}
        results = dev_runner.controlled_points([failure], [control])
        self.assertEqual(len(results), 1)
        self.assert_result(results[0], 'unknown', 'probe_error')

    def test_successful_control_preserves_failed_measurement(self):
        failure = {'state': 'fail', 'reason': 'timeout'}
        good = {'state': 'pass', 'reason': 'ok'}
        self.assertEqual(dev_runner.controlled_points([failure], [good]), [failure])


if __name__ == '__main__':
    unittest.main()
