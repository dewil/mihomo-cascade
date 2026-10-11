"""Independent pure summarize_unknown_intervals public-contract tests.

Initial RED: the public helper was absent (17 tests, 23 subcase failures).
This records a missing-contract RED, not an existing semantic implementation bug.

Implementation source is not read. Fixtures are synthetic evaluations; no live
runner, SSH, subprocess, network or fault is invoked. Durations are independently
calculated interval lengths, not sums of unknown-cell milliseconds.
"""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dev_runner

A = ('llm', 'de4', 'transport')
B = ('llm', 'de4', 'application')
OUTSIDE = ('ru', 'de4', 'application')


def snapshot(at, states):
    return {'evaluation': {'evaluated_at_ms': at, 'cells': [
        {'observer': key[0], 'target': key[1], 'check': key[2],
         'state': state, 'reason': 'probe_error' if state == 'unknown' else 'ok'}
        for key, state in states.items()]}, 'irrelevant': {'state': 'unknown'}}


class DevUnknownIntervalContract(unittest.TestCase):
    def summary(self, samples, start=0, end=30, scope=(A,)):
        self.assertTrue(callable(getattr(dev_runner, 'summarize_unknown_intervals', None)),
                        'Missing public summarize_unknown_intervals contract')
        result = dev_runner.summarize_unknown_intervals(
            samples, start_ms=start, end_ms=end, relevant_cells=list(scope))
        self.assertEqual(set(result), {
            'unknown_sample_hold_ms', 'full_matrix_unknown_sample_hold_ms',
            'uncovered_prefix_ms', 'max_evaluation_gap_ms', 'scope_cells',
            'excluded_cells', 'method'})
        self.assertIn('sample-and-hold', result['method'])
        return result

    def test_outside_unknown_counts_full_matrix_but_not_relevant_scope(self):
        samples = [snapshot(0, {A: 'pass', OUTSIDE: 'unknown'}),
                   snapshot(30, {A: 'pass', OUTSIDE: 'unknown'})]
        result = self.summary(samples)
        self.assertEqual(result['unknown_sample_hold_ms'], 0)
        self.assertEqual(result['full_matrix_unknown_sample_hold_ms'], 30)
        self.assertEqual(result['scope_cells'], [list(A)])
        self.assertEqual(result['excluded_cells'], [list(OUTSIDE)])
        self.assertEqual(result['uncovered_prefix_ms'], 0)

    def test_unknown_at_ten_until_pass_at_twenty_is_ten_ms(self):
        samples = [snapshot(0, {A: 'pass'}), snapshot(10, {A: 'unknown'}),
                   snapshot(20, {A: 'pass'}), snapshot(30, {A: 'pass'})]
        result = self.summary(samples)
        self.assertEqual(result['unknown_sample_hold_ms'], 10)
        self.assertEqual(result['full_matrix_unknown_sample_hold_ms'], 10)
        self.assertEqual(result['max_evaluation_gap_ms'], 10)

    def test_overlapping_unknown_cells_are_not_double_counted(self):
        samples = [snapshot(0, {A: 'pass', B: 'pass'}),
                   snapshot(10, {A: 'unknown', B: 'unknown'}),
                   snapshot(20, {A: 'pass', B: 'unknown'}),
                   snapshot(30, {A: 'pass', B: 'pass'})]
        result = self.summary(samples, scope=(B, A))
        self.assertEqual(result['unknown_sample_hold_ms'], 20)
        self.assertEqual(result['full_matrix_unknown_sample_hold_ms'], 20)
        self.assertEqual(result['scope_cells'], sorted([list(A), list(B)]))

    def test_baseline_before_start_is_held_and_clipped(self):
        samples = [snapshot(0, {A: 'unknown'}), snapshot(10, {A: 'pass'}),
                   snapshot(30, {A: 'pass'})]
        result = self.summary(samples, start=5, end=25)
        self.assertEqual(result['unknown_sample_hold_ms'], 5)
        self.assertEqual(result['uncovered_prefix_ms'], 0)
        self.assertEqual(result['max_evaluation_gap_ms'], 20)

    def test_long_recovery_gap_is_reported_and_unknown_is_held(self):
        samples = [snapshot(0, {A: 'pass'}), snapshot(10, {A: 'unknown'}),
                   snapshot(110, {A: 'pass'})]
        result = self.summary(samples, end=110)
        self.assertEqual(result['unknown_sample_hold_ms'], 100)
        self.assertEqual(result['max_evaluation_gap_ms'], 100)

    def test_missing_required_cell_is_unknown(self):
        samples = [snapshot(0, {A: 'pass'}), snapshot(10, {A: 'pass', B: 'pass'}),
                   snapshot(30, {A: 'pass', B: 'pass'})]
        result = self.summary(samples, scope=(A, B))
        self.assertEqual(result['unknown_sample_hold_ms'], 10)
        self.assertEqual(result['full_matrix_unknown_sample_hold_ms'], 10)

    def test_no_anchor_reports_uncovered_prefix_without_inventing_state(self):
        samples = [snapshot(10, {A: 'unknown'}), snapshot(20, {A: 'pass'}),
                   snapshot(30, {A: 'pass'})]
        result = self.summary(samples)
        self.assertEqual(result['uncovered_prefix_ms'], 10)
        self.assertEqual(result['unknown_sample_hold_ms'], 10)
        self.assertEqual(result['full_matrix_unknown_sample_hold_ms'], 10)

    def test_failed_state_is_known_not_unknown(self):
        result = self.summary([snapshot(0, {A: 'fail'}), snapshot(30, {A: 'fail'})])
        self.assertEqual(result['unknown_sample_hold_ms'], 0)
        self.assertEqual(result['full_matrix_unknown_sample_hold_ms'], 0)

    def test_unsorted_samples_and_normalized_identical_duplicates_collapse(self):
        first = snapshot(0, {A: 'unknown', B: 'pass'})
        same = snapshot(0, {B: 'pass', A: 'unknown'})
        same['irrelevant'] = 'different unrelated metadata'
        final = snapshot(30, {A: 'pass', B: 'pass'})
        result = self.summary([final, same, first], scope=(A, B))
        self.assertEqual(result['unknown_sample_hold_ms'], 30)
        self.assertEqual(result['max_evaluation_gap_ms'], 30)

    def test_conflicting_same_time_state_or_reason_is_rejected(self):
        for field, changed in [('state', 'unknown'), ('reason', 'different_reason')]:
            with self.subTest(field=field):
                first = snapshot(0, {A: 'pass'})
                conflict = copy.deepcopy(first)
                conflict['evaluation']['cells'][0][field] = changed
                with self.assertRaises(ValueError):
                    self.summary([first, conflict, snapshot(30, {A: 'pass'})])

    def test_duplicate_cell_ids_are_rejected_even_when_identical(self):
        sample = snapshot(0, {A: 'pass'})
        sample['evaluation']['cells'].append(copy.deepcopy(sample['evaluation']['cells'][0]))
        with self.assertRaises(ValueError):
            self.summary([sample, snapshot(30, {A: 'pass'})])

    def test_boolean_and_negative_evaluation_timestamps_are_rejected(self):
        for at in [True, False, -1]:
            with self.subTest(at=at), self.assertRaises(ValueError):
                self.summary([snapshot(at, {A: 'pass'}), snapshot(30, {A: 'pass'})])

    def test_boolean_and_negative_window_timestamps_are_rejected(self):
        samples = [snapshot(0, {A: 'pass'}), snapshot(30, {A: 'pass'})]
        for start, end in [(True, 30), (-1, 30), (0, False), (0, -1)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.summary(samples, start=start, end=end)

    def test_end_after_last_evaluation_is_rejected_no_extrapolation(self):
        with self.assertRaises(ValueError):
            self.summary([snapshot(0, {A: 'unknown'}), snapshot(20, {A: 'pass'})], end=21)

    def test_end_before_start_is_rejected(self):
        with self.assertRaises(ValueError):
            self.summary([snapshot(0, {A: 'pass'}), snapshot(30, {A: 'pass'})], start=20, end=10)

    def test_zero_length_window_has_no_unknown_duration(self):
        result = self.summary([snapshot(0, {A: 'unknown'}), snapshot(30, {A: 'pass'})],
                              start=10, end=10)
        self.assertEqual(result['unknown_sample_hold_ms'], 0)
        self.assertEqual(result['full_matrix_unknown_sample_hold_ms'], 0)
        self.assertEqual(result['uncovered_prefix_ms'], 0)

    def test_pure_function_does_not_mutate_samples(self):
        samples = [snapshot(30, {A: 'pass'}), snapshot(0, {A: 'unknown'})]
        before = copy.deepcopy(samples)
        self.summary(samples)
        self.assertEqual(samples, before)


if __name__ == '__main__':
    unittest.main()
