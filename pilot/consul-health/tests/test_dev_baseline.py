"""Independent baseline_ready contract tests; no implementation source read.

Initial RED observed: baseline_ready was absent (missing public contract),
not a claimed semantic failure of an existing implementation.

Only the pure function is invoked. No Core, client, SSH, fault or network I/O.
Required catalogue evidence is llm transport/application/accounting and
ru/ru2 transport. Non-required application/accounting cells do not gate baseline.
"""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev_runner


REQUIRED = [('llm', 'transport'), ('llm', 'application'), ('llm', 'accounting'),
            ('ru', 'transport'), ('ru2', 'transport')]


def passed():
    return {'state': 'pass', 'reason': 'ok'}


def baseline_sample():
    cells = [dict(observer=observer, target='de4', check=check, **passed())
             for observer, check in REQUIRED]
    cells.extend(dict(observer=observer, target='de4', check=check,
                      state='unknown', reason='probe_error')
                 for observer in ['ru', 'ru2']
                 for check in ['application', 'accounting'])
    return {'application': passed(), 'accounting': passed(),
            'transport': {observer: passed() for observer in ['llm', 'ru', 'ru2']},
            'evaluation': {'control_plane': 'ok', 'cells': cells}}


class DevBaselineContract(unittest.TestCase):
    def ready(self, sample):
        self.assertTrue(callable(getattr(dev_runner, 'baseline_ready', None)),
                        'Missing public baseline_ready(sample) contract')
        return dev_runner.baseline_ready(sample)

    def test_complete_direct_and_required_catalog_pass_is_ready(self):
        self.assertIs(self.ready(baseline_sample()), True)

    def test_unrequired_ru_application_and_accounting_unknown_do_not_block(self):
        sample = baseline_sample()
        self.assertEqual(sum(cell['state'] == 'unknown'
                             for cell in sample['evaluation']['cells']), 4)
        self.assertIs(self.ready(sample), True)

    def test_every_direct_failure_or_unknown_blocks_ready_catalog(self):
        for name in ['application', 'accounting', 'llm', 'ru', 'ru2']:
            for state, reason in [('fail', 'timeout'), ('unknown', 'probe_error')]:
                with self.subTest(name=name, state=state):
                    sample = baseline_sample()
                    target = sample if name in ['application', 'accounting'] else sample['transport']
                    target[name] = {'state': state, 'reason': reason}
                    self.assertIs(self.ready(sample), False)

    def test_each_missing_required_catalog_cell_blocks(self):
        for observer, check in REQUIRED:
            with self.subTest(observer=observer, check=check):
                sample = baseline_sample()
                sample['evaluation']['cells'] = [cell for cell in sample['evaluation']['cells']
                    if (cell['observer'], cell['check']) != (observer, check)]
                self.assertIs(self.ready(sample), False)

    def test_fresh_direct_pass_does_not_mask_failed_unknown_or_stale_catalog(self):
        for observer, check in REQUIRED:
            for state, reason in [('fail', 'timeout'), ('unknown', 'probe_error'),
                                  ('unknown', 'stale')]:
                with self.subTest(observer=observer, check=check, reason=reason):
                    sample = baseline_sample()
                    cell = next(cell for cell in sample['evaluation']['cells']
                                if (cell['observer'], cell['check']) == (observer, check))
                    cell.update(state=state, reason=reason)
                    self.assertIs(self.ready(sample), False)

    def test_conflicting_duplicate_required_cell_blocks_regardless_of_order(self):
        for reverse in [False, True]:
            with self.subTest(reverse=reverse):
                sample = baseline_sample()
                duplicate = dict(observer='llm', target='de4', check='application',
                                 state='unknown', reason='stale')
                sample['evaluation']['cells'].append(duplicate)
                if reverse:
                    sample['evaluation']['cells'].reverse()
                self.assertIs(self.ready(sample), False)

    def test_other_target_evidence_does_not_replace_required_de4_cell(self):
        sample = baseline_sample()
        sample['evaluation']['cells'][0]['target'] = 'other-node'
        self.assertIs(self.ready(sample), False)

    def test_unavailable_control_plane_blocks_even_when_all_probes_pass(self):
        sample = baseline_sample()
        sample['evaluation']['control_plane'] = 'unavailable'
        self.assertIs(self.ready(sample), False)

    def test_catalog_empty_blocks_even_when_all_direct_probes_pass(self):
        sample = baseline_sample()
        sample['evaluation']['cells'] = []
        self.assertIs(self.ready(sample), False)

    def test_pure_function_does_not_change_input(self):
        sample = baseline_sample()
        before = copy.deepcopy(sample)
        self.assertIs(self.ready(sample), True)
        self.assertEqual(sample, before)


if __name__ == '__main__':
    unittest.main()
