"""动作展开和顺序依赖用例，由用户手动执行。"""

import unittest

from ai_edge.semantic.semantic_contract import (
    SemanticError,
    empty_candidate,
    validate_candidate,
)
from ai_edge.task_drafts.actions import action, action_steps, validate_action_plan


class TaskActionTests(unittest.TestCase):
    def setUp(self):
        self.stations, self.objects = ["station_a", "station_b"], ["box_1"]
        self.transport = empty_candidate("TRANSPORT")
        self.transport.update(
            object_id="box_1",
            pickup_station_id="station_a",
            dropoff_station_id="station_b",
        )

    def test_transport_expands_to_four_actions_with_matching_parameters(self):
        plan = validate_action_plan(self.transport, self.stations, self.objects)
        steps = plan["steps"]
        self.assertEqual(
            [s["task_type"] for s in steps], ["NAVIGATE", "PICK", "NAVIGATE", "PLACE"]
        )
        self.assertEqual(steps[0]["destination_station_id"], "station_a")
        self.assertEqual(steps[1]["pickup_station_id"], "station_a")
        self.assertEqual(steps[1]["object_id"], "box_1")
        self.assertEqual(steps[2]["destination_station_id"], "station_b")
        self.assertEqual(steps[3]["dropoff_station_id"], "station_b")
        self.assertEqual(steps[3]["object_id"], "box_1")

    def test_already_expanded_plan_is_not_expanded_again(self):
        plan = validate_action_plan(self.transport, self.stations, self.objects)
        self.assertEqual(validate_action_plan(plan, self.stations, self.objects), plan)

    def test_return_is_a_separate_navigation_after_placing(self):
        self.transport.update(return_required=True, return_station_id="station_a")
        steps = action_steps(self.transport)
        self.assertEqual(len(steps), 5)
        self.assertEqual(steps[-2]["task_type"], "PLACE")
        self.assertEqual(steps[-1]["destination_station_id"], "station_a")

    def test_multiple_navigation_and_gripper_actions_keep_user_order(self):
        steps = [
            action("NAVIGATE", destination_station_id="station_a"),
            action("NAVIGATE", destination_station_id="station_b"),
            action("PICK", object_id="box_1", pickup_station_id="station_b"),
            action("PLACE", object_id="box_1", dropoff_station_id="station_b"),
        ]
        candidate = empty_candidate("SEQUENCE", steps)
        self.assertEqual(
            validate_action_plan(candidate, self.stations, self.objects), candidate
        )

    def test_missing_object_is_not_filled_from_whitelist(self):
        self.transport["object_id"] = ""
        with self.assertRaises(SemanticError) as caught:
            validate_action_plan(self.transport, self.stations, self.objects)
        self.assertEqual(caught.exception.code, "MISSING_FIELDS")

    def test_wrong_navigation_before_placing_is_rejected(self):
        steps = action_steps(self.transport)
        steps[2]["destination_station_id"] = "station_a"
        with self.assertRaises(SemanticError) as caught:
            validate_action_plan(
                empty_candidate("SEQUENCE", steps), self.stations, self.objects
            )
        self.assertEqual(caught.exception.code, "INVALID_ACTION_ORDER")

    def test_place_without_prior_pick_is_rejected(self):
        candidate = empty_candidate("PLACE")
        candidate.update(object_id="box_1", dropoff_station_id="station_a")
        with self.assertRaises(SemanticError) as caught:
            validate_action_plan(candidate, self.stations, self.objects)
        self.assertEqual(caught.exception.code, "INVALID_ACTION_ORDER")

    def test_over_limit_after_expansion_is_rejected_without_truncating(self):
        step = {key: value for key, value in self.transport.items() if key != "steps"}
        with self.assertRaises(SemanticError) as caught:
            action_steps(empty_candidate("SEQUENCE", [step] * 65))
        self.assertEqual(caught.exception.code, "PLAN_TOO_LONG")

    def test_semantic_contract_accepts_direct_pick_and_place(self):
        pick = empty_candidate("PICK")
        pick.update(object_id="box_1", pickup_station_id="station_a")
        answer = validate_candidate(pick, self.stations, self.objects)
        self.assertFalse(answer.missing_fields)
        place = empty_candidate("PLACE")
        place.update(object_id="box_1", dropoff_station_id="station_b")
        self.assertFalse(
            validate_candidate(place, self.stations, self.objects).missing_fields
        )


if __name__ == "__main__":
    unittest.main()
