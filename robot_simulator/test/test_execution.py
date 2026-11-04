"""Model-level execution tests, with virtual time and no ROS or real hardware."""

import unittest

from robot_simulator.execution import ExecutionError, SimulationExecution

TRANSPORT = [
    ("NAVIGATE", "station_a", ""),
    ("PICK", "station_a", "box_1"),
    ("NAVIGATE", "station_b", ""),
    ("PLACE", "station_b", "box_1"),
]


def advance_until(model, predicate):
    for _ in range(3000):
        if predicate():
            return
        if model.state != "RUNNING":
            raise AssertionError(model.frame())
        model.tick(0.1)
    raise AssertionError("virtual execution did not complete")


class ExecutionTests(unittest.TestCase):
    def test_catalog_ids_replace_default_scene_and_complete_user_named_transport(self):
        model = SimulationExecution(speed=1.0, action_seconds=0.2)
        model.sync_catalog(["1", "2"], ["x"], initial=True)
        self.assertEqual(set(model.stations), {"1", "2"})
        self.assertEqual(model.objects, {"x": "1"})
        model.start(
            [
                ("NAVIGATE", "1", ""),
                ("PICK", "1", "x"),
                ("NAVIGATE", "2", ""),
                ("PLACE", "2", "x"),
            ]
        )
        advance_until(model, lambda: model.state == "SUCCEEDED")
        self.assertEqual(model.objects["x"], "2")
        location = model.stations["2"]
        model.sync_catalog(["1", "2", "3"], ["x", "y"])
        self.assertEqual(model.stations["2"], location)
        self.assertEqual(model.objects["x"], "2")
        self.assertEqual(model.objects["y"], "1")
        before = model.frame()
        with self.assertRaises(ExecutionError):
            model.sync_catalog(["1", "3"], ["x", "y"])
        self.assertEqual(model.frame(), before)

    def test_catalog_sync_never_changes_an_active_task_or_held_material(self):
        model = SimulationExecution(speed=1.0, action_seconds=0.2)
        model.sync_catalog(["1", "2"], ["x"], initial=True)
        model.start([("NAVIGATE", "1", ""), ("PICK", "1", "x")])
        with self.assertRaises(ExecutionError):
            model.sync_catalog(["1", "2", "3"], ["x"])
        advance_until(model, lambda: model.state == "SUCCEEDED")
        self.assertEqual(model.held_object, "x")
        with self.assertRaises(ExecutionError):
            model.sync_catalog(["1", "2"], [])
        self.assertEqual(model.held_object, "x")

    def model(self, **parameters):
        return SimulationExecution(speed=1.0, action_seconds=0.2, **parameters)

    def test_navigation_pick_navigation_place_share_world_and_feedback(self):
        model = self.model()
        model.start(TRANSPORT)
        model.tick(0.5)
        self.assertAlmostEqual(model.x, -0.5)
        self.assertEqual(model.completed, 0)
        self.assertEqual(model.objects["box_1"], "station_a")
        advance_until(model, lambda: model.completed == 1)
        self.assertAlmostEqual(model.x, -2.0)
        self.assertEqual(model.velocity, 0.0)
        model.tick(0.1)
        self.assertEqual(model.held_object, "")
        model.tick(0.1)
        self.assertEqual(model.held_object, "box_1")
        self.assertTrue(model.frame()["pickup_completed"])
        model.tick(0.5)
        frame = model.frame()
        self.assertAlmostEqual(frame["x"], -1.5)
        self.assertEqual(frame["objects"][0]["x"], frame["x"])
        self.assertTrue(frame["objects"][0]["carried"])
        advance_until(model, lambda: model.completed == 3)
        self.assertAlmostEqual(model.x, 2.0)
        self.assertEqual(model.held_object, "box_1")
        model.tick(0.2)
        self.assertEqual(model.state, "SUCCEEDED")
        self.assertEqual(model.progress, 1.0)
        self.assertEqual(model.objects["box_1"], "station_b")
        self.assertEqual(model.held_object, "")
        self.assertTrue(model.frame()["dropoff_completed"])
        self.assertEqual(model.velocity, 0.0)
        terminal = model.frame()
        model.cancel()
        model.fail("LATE_ERROR", "迟到的错误不能改变完成终态")
        model.tick(10.0)
        self.assertEqual(model.frame(), terminal)

    def test_cancel_navigation_freezes_position_progress_and_materials(self):
        model = self.model()
        model.start(TRANSPORT)
        model.tick(0.4)
        model.cancel()
        frame = model.frame()
        model.tick(100.0)
        self.assertEqual(model.frame(), frame)
        self.assertEqual(frame["state"], "CANCELED")
        self.assertGreater(frame["progress"], 0.0)
        self.assertLess(frame["progress"], 1.0)
        self.assertEqual(frame["speed"], 0.0)
        self.assertEqual(model.objects["box_1"], "station_a")

    def test_cancel_before_pick_commit_does_not_claim_object(self):
        model = self.model()
        model.start(TRANSPORT)
        model.tick(2.0)
        model.tick(0.1)
        model.cancel()
        model.tick(10.0)
        self.assertEqual(model.held_object, "")
        self.assertEqual(model.objects["box_1"], "station_a")
        self.assertFalse(model.frame()["pickup_completed"])

    def test_fault_after_pick_preserves_held_object_and_skips_place(self):
        model = self.model(fail_step=3)
        model.start(TRANSPORT)
        model.tick(2.0)
        model.tick(0.2)
        model.tick(0.1)
        self.assertEqual(model.state, "FAILED")
        self.assertEqual(model.error_code, "SIMULATED_FAILURE")
        self.assertEqual(model.completed, 2)
        self.assertEqual(model.held_object, "box_1")
        self.assertFalse(model.frame()["dropoff_completed"])
        old = model.frame()
        model.tick(100.0)
        self.assertEqual(model.frame(), old)

    def test_wall_timeout_stops_without_teleport_or_side_effect(self):
        model = self.model(step_timeout=1.0)
        model.start(TRANSPORT)
        model.tick(0.02, wall_dt=2.0)
        self.assertEqual(model.error_code, "STEP_TIMEOUT")
        self.assertEqual(model.x, 0.0)
        self.assertEqual(model.velocity, 0.0)
        self.assertEqual(model.held_object, "")

    def test_invalid_plan_never_mutates_world(self):
        model = self.model()
        for steps, code in (
            ([("NAVIGATE", "unknown", "")], "UNKNOWN_STATION"),
            ([("PICK", "station_a", "box_1")], "NOT_AT_STATION"),
            (
                [("NAVIGATE", "station_a", ""), ("PLACE", "station_a", "box_1")],
                "NOT_HOLDING_OBJECT",
            ),
            (
                [("NAVIGATE", "station_a", ""), ("PICK", "station_a", "unknown")],
                "UNKNOWN_OBJECT",
            ),
        ):
            with self.assertRaises(ExecutionError) as error:
                model.start(steps)
            self.assertEqual(error.exception.code, code)
            self.assertEqual(model.state, "IDLE")
            self.assertEqual(model.x, 0.0)

    def test_inventory_persists_between_tasks_and_can_be_transported_back(self):
        model = self.model()
        model.start(TRANSPORT)
        advance_until(model, lambda: model.state == "SUCCEEDED")
        with self.assertRaises(ExecutionError) as error:
            model.start(TRANSPORT)
        self.assertEqual(error.exception.code, "OBJECT_NOT_AT_STATION")
        model.start(
            [
                ("PICK", "station_b", "box_1"),
                ("NAVIGATE", "station_a", ""),
                ("PLACE", "station_a", "box_1"),
            ]
        )
        advance_until(model, lambda: model.state == "SUCCEEDED")
        self.assertEqual(model.objects["box_1"], "station_a")

    def test_long_navigation_sequence_and_return_flag(self):
        model = self.model()
        steps = [
            ("NAVIGATE", "station_a" if i % 2 == 0 else "station_b", "")
            for i in range(64)
        ]
        model.start(steps, return_steps={63})
        for _ in range(64):
            model.tick(5.0)
        self.assertEqual(model.completed, 64)
        self.assertEqual(model.state, "SUCCEEDED")
        self.assertTrue(model.frame()["return_completed"])

    def test_multiple_pick_place_flags_only_complete_after_all_matching_steps(self):
        model = self.model(objects={"box_1": "station_a", "box_2": "station_b"})
        steps = TRANSPORT + [
            ("PICK", "station_b", "box_2"),
            ("NAVIGATE", "station_a", ""),
            ("PLACE", "station_a", "box_2"),
        ]
        model.start(steps)
        advance_until(model, lambda: model.completed == 4)
        self.assertFalse(model.frame()["pickup_completed"])
        self.assertFalse(model.frame()["dropoff_completed"])
        advance_until(model, lambda: model.state == "SUCCEEDED")
        self.assertTrue(model.frame()["pickup_completed"])
        self.assertTrue(model.frame()["dropoff_completed"])


if __name__ == "__main__":
    unittest.main()
