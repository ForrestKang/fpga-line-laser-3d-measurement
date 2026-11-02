"""Pure metric tests: no cv2, PYNQ, HDMI, DMA or filesystem dependency."""
import math
import unittest
from .metrics import sliding_windows, new_model_acks, display_metrics


class SlidingWindowTests(unittest.TestCase):
    def test_fixed_bins_miss_complete_middle_stall(self):
        events = [i*.049 for i in range(50)]+[7.55+i*.049 for i in range(50)]
        self.assertEqual([sum(s <= t < s+5 for t in events) for s in (0, 5)], [50, 50])
        report = sliding_windows(events, 0, 10)
        self.assertTrue(report['evaluated'])
        self.assertEqual(report['worst_hz'], 0)

    def test_half_open_boundaries_and_duplicates(self):
        r = sliding_windows([0, 0, 5, 10], 0, 10)
        self.assertEqual(r['worst_count'], 1)
        self.assertEqual(r['best_count'], 2)
        self.assertEqual(r['event_count'], 3)
        self.assertEqual(sliding_windows([0, 5], 0, 5)['worst_count'], 1)

    def test_after_boundary_is_not_lost_to_epsilon(self):
        r = sliding_windows([0, math.nextafter(5., math.inf)], 0, 6)
        self.assertEqual(r['worst_count'], 0)

    def test_run_shorter_than_width_not_vacuous_pass(self):
        r = sliding_windows([1, 2], 0, 4.999999999)
        self.assertEqual(r['status'], 'NOT_EVALUATED')
        self.assertIsNone(r['worst_hz'])
        self.assertEqual(r['candidate_windows'], 0)

    def test_empty_full_run_has_zero_rate(self):
        self.assertEqual(sliding_windows([], 20, 30)['worst_hz'], 0)

    def test_translation_and_unsorted_events(self):
        a = sliding_windows([9, 0, 3, 6], 0, 10)
        b = sliding_windows([109, 100, 103, 106], 100, 110)
        self.assertEqual(a['worst_count'], b['worst_count'])
        self.assertEqual(a['best_count'], b['best_count'])

    def test_invalid_inputs_rejected(self):
        for args in [([], 0, 10, 0), ([], 10, 0, 5), ([float('nan')], 0, 10, 5),
                     ([], True, 10, 5), ([], 0, float('inf'), 5)]:
            with self.assertRaises(ValueError):
                sliding_windows(*args)


def row(frame, points, t, valid=True, age=.1):
    ack = int(t*1e9)
    return dict(target='T05', frame_id=frame, cloud_points=points, ack_perf_s=t,
                receipt=dict(valid=valid), ack_monotonic_ns=ack,
                source_capture_monotonic_ns=ack-int(age*1e9))


class DisplayMetricTests(unittest.TestCase):
    @staticmethod
    def valid(receipt):
        return isinstance(receipt, dict) and receipt.get('valid') is True

    def test_only_valid_unique_growing_models_count_for_both_metrics(self):
        records = [row(0, 2, 1, age=.1), row(0, 2, 1.1, age=8),
                   row(1, 2, 1.2, age=8), row(2, 3, 1.3, valid=False, age=8),
                   row(2, 3, 1.4, age=.3)]
        self.assertEqual(new_model_acks(records, self.valid), [1., 1.4])
        r = display_metrics(records, self.valid)
        self.assertEqual(r['new_model_ack_count'], 2)
        self.assertEqual(r['age_samples'], 2)
        self.assertAlmostEqual(r['display_age_p95_s'], .29)
        self.assertTrue(r['age_population_complete'])

    def test_seen_no_growth_identity_cannot_reappear_as_new_model(self):
        records = [row(0, 2, 1), row(1, 2, 2), row(1, 4, 3), row(2, 5, 4)]
        self.assertEqual(new_model_acks(records, self.valid), [1., 4.])

    def test_missing_age_is_partial_not_silently_complete(self):
        records = [row(0, 2, 1), row(1, 4, 2)]
        del records[1]['source_capture_monotonic_ns']
        r = display_metrics(records, self.valid)
        self.assertEqual(r['new_model_ack_count'], 2)
        self.assertEqual(r['age_status'], 'PARTIAL')
        self.assertEqual(r['age_missing_count'], 1)
        self.assertFalse(r['age_population_complete'])

    def test_no_valid_models_not_evaluated(self):
        r = display_metrics([row(0, 2, 1, valid=False)], self.valid)
        self.assertEqual(r['age_status'], 'NOT_EVALUATED')
        self.assertIsNone(r['display_age_p95_s'])
        self.assertIsNone(r['new_model_ack_rate_hz'])

    def test_negative_age_and_backwards_clock_rejected(self):
        with self.assertRaisesRegex(ValueError, 'precedes'):
            display_metrics([row(0, 2, 1, age=-1)], self.valid)
        with self.assertRaisesRegex(ValueError, 'backwards'):
            new_model_acks([row(0, 2, 2), row(1, 3, 1)], self.valid)

    def test_nan_ack_and_bad_point_count_rejected(self):
        r = row(0, 2, 1); r['ack_perf_s'] = float('nan')
        with self.assertRaises(ValueError):
            new_model_acks([r], self.valid)
        r = row(0, 2, 1); r['cloud_points'] = True
        with self.assertRaises(ValueError):
            new_model_acks([r], self.valid)


if __name__ == '__main__':
    unittest.main()
