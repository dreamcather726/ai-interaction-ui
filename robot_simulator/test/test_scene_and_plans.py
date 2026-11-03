"""Pure tests; ROS integration is exercised separately in the target Humble workspace."""

from types import SimpleNamespace
import unittest

from robot_simulator.plans import describe_step, expand_steps, return_step_indices
from robot_simulator.scene import occupancy_grid, patrol, rgb_frame


def candidate(kind=2, **changes):
    value = dict(
        UNKNOWN=0,
        NAVIGATE=1,
        TRANSPORT=2,
        SEQUENCE=3,
        PICK=4,
        PLACE=5,
        task_type=kind,
        object_id="box_1",
        pickup_station_id="station_a",
        dropoff_station_id="station_b",
        destination_station_id="station_a",
        return_required=False,
        return_station_id="",
        steps=[],
    )
    value.update(changes)
    return SimpleNamespace(**value)


class SceneAndPlanTests(unittest.TestCase):
    def test_map_has_correct_occupancy_shape(self):
        data = occupancy_grid()
        self.assertEqual(len(data), 200 * 200)
        self.assertEqual(set(data), {0, 100})
        self.assertEqual(data[100 * 200 + 60], 0)  # A position.
        self.assertEqual(data[100 * 200 + 140], 0)  # B position.
        self.assertEqual(data[70 * 200 + 100], 100)  # Workcell wall.

    def test_rgb_has_exact_stride_and_changes_with_motion_and_clock(self):
        first = rgb_frame(160, 120, 0.0, "12:00:00")
        second = rgb_frame(160, 120, 1.0, "12:00:01")
        self.assertEqual(len(first), 160 * 120 * 3)
        self.assertNotEqual(first, second)
        self.assertGreater(len(set(first)), 10)
        self.assertEqual(patrol(0)[0], -2.0)
        self.assertAlmostEqual(patrol(4 / 0.35)[0], 2.0)

    def test_transport_expands_to_four_actions_in_order(self):
        steps = expand_steps(candidate())
        self.assertEqual(
            [step[0] for step in steps], ["NAVIGATE", "PICK", "NAVIGATE", "PLACE"]
        )
        self.assertEqual(steps[0][1], "station_a")
        self.assertEqual(steps[-1][1:], ("station_b", "box_1"))
        self.assertIn("4/4", describe_step(4, 4, steps[-1]))

    def test_variable_length_and_return_are_not_truncated(self):
        plan = candidate(3, steps=[candidate(1) for _ in range(25)])
        self.assertEqual(len(expand_steps(plan)), 25)
        returning = candidate(return_required=True, return_station_id="station_a")
        self.assertEqual(expand_steps(returning)[-1], ("NAVIGATE", "station_a", ""))
        self.assertEqual(return_step_indices(returning), {4})
        with self.assertRaises(ValueError):
            expand_steps(candidate(3, steps=[candidate() for _ in range(65)]))

    def test_incomplete_task_is_rejected_before_execution(self):
        with self.assertRaises(ValueError):
            expand_steps(candidate(object_id=""))
        with self.assertRaises(ValueError):
            expand_steps(candidate(3, steps=[]))


if __name__ == "__main__":
    unittest.main()
