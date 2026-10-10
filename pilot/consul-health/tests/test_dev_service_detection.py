"""Independent pure service_down_detected contract; implementation unread.

Unknown application evidence remains unknown. Definite local HAProxy absence
and three failed transports, with two passing controls, establish the outage.
No Client/Core construction, subprocess, SSH, network or live fault calls.
"""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev_runner


def result(state, reason):
    return {'state': state, 'reason': reason}


def outage_sample():
    return {
        'local_services': {
            'units_active': {'hiddify-haproxy.service': False},
            'public443_haproxy': False,
        },
        'transport': {name: result('fail', 'connect_failed')
                      for name in ['llm', 'ru', 'ru2']},
        'application': result('unknown', 'probe_error'),
        'control_points': [result('pass', 'ok'), result('pass', 'ok')],
    }


class DevServiceDetectionContract(unittest.TestCase):
    def detected(self, sample):
        self.assertTrue(callable(getattr(dev_runner, 'service_down_detected', None)),
                        'Missing public service_down_detected(sample) contract')
        return dev_runner.service_down_detected(sample)

    def test_definite_local_and_transport_failure_accepts_unknown_application(self):
        self.assertIs(self.detected(outage_sample()), True)

    def test_failed_application_also_allows_definite_outage(self):
        sample = outage_sample()
        sample['application'] = result('fail', 'timeout')
        self.assertIs(self.detected(sample), True)

    def test_passing_application_blocks_outage(self):
        sample = outage_sample()
        sample['application'] = result('pass', 'ok')
        self.assertIs(self.detected(sample), False)

    def test_unknown_application_is_not_rewritten_as_failure(self):
        sample = outage_sample()
        before = copy.deepcopy(sample)
        self.assertIs(self.detected(sample), True)
        self.assertEqual(sample, before)
        self.assertEqual(sample['application'], result('unknown', 'probe_error'))

    def test_local_flags_must_be_explicitly_false(self):
        for field in ['unit', 'public443']:
            for value in [True, None, 'unknown', 0]:
                with self.subTest(field=field, value=value):
                    sample = outage_sample()
                    if field == 'unit':
                        sample['local_services']['units_active']['hiddify-haproxy.service'] = value
                    else:
                        sample['local_services']['public443_haproxy'] = value
                    self.assertIs(self.detected(sample), False)

    def test_missing_local_flags_block_outage(self):
        for field in ['local_services', 'units_active', 'unit', 'public443_haproxy']:
            with self.subTest(field=field):
                sample = outage_sample()
                if field == 'local_services':
                    del sample[field]
                elif field == 'unit':
                    del sample['local_services']['units_active']['hiddify-haproxy.service']
                else:
                    del sample['local_services'][field]
                self.assertIs(self.detected(sample), False)

    def test_each_transport_must_fail_not_pass_or_unknown(self):
        for observer in ['llm', 'ru', 'ru2']:
            for state, reason in [('pass', 'ok'), ('unknown', 'probe_error')]:
                with self.subTest(observer=observer, state=state):
                    sample = outage_sample()
                    sample['transport'][observer] = result(state, reason)
                    self.assertIs(self.detected(sample), False)

    def test_each_required_transport_name_must_exist(self):
        for observer in ['llm', 'ru', 'ru2']:
            with self.subTest(observer=observer):
                sample = outage_sample()
                del sample['transport'][observer]
                sample['transport']['other-observer'] = result('fail', 'connect_failed')
                self.assertIs(self.detected(sample), False)

    def test_both_direct_controls_must_pass(self):
        for index in [0, 1]:
            for state, reason in [('fail', 'timeout'), ('unknown', 'probe_error')]:
                with self.subTest(index=index, state=state):
                    sample = outage_sample()
                    sample['control_points'][index] = result(state, reason)
                    self.assertIs(self.detected(sample), False)

    def test_missing_control_does_not_prove_outage(self):
        for controls in [[], [result('pass', 'ok')]]:
            with self.subTest(count=len(controls)):
                sample = outage_sample()
                sample['control_points'] = controls
                self.assertIs(self.detected(sample), False)

    def test_unknown_evidence_alone_does_not_prove_outage(self):
        sample = outage_sample()
        sample['local_services']['units_active']['hiddify-haproxy.service'] = None
        sample['local_services']['public443_haproxy'] = None
        sample['transport'] = {name: result('unknown', 'probe_error')
                               for name in ['llm', 'ru', 'ru2']}
        self.assertIs(self.detected(sample), False)


if __name__ == '__main__':
    unittest.main()
