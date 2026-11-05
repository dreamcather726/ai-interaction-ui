"""交互协议、教学答案及实时查询的离线回归。"""

import json
import unittest

from ai_edge.semantic.interaction_contract import validate_interaction
from ai_edge.semantic.semantic_contract import SemanticError, empty_candidate
from ai_edge.semantic.semantic_prompt import build_interaction_messages
from ai_edge.semantic.ollama_backend import OllamaBackend
from ai_edge.semantic.robot_state_query import RobotStateCache


class InteractionTests(unittest.TestCase):
    def envelope(self, intent="CHAT", reply="你好！", queries=None, tasks=None):
        return dict(
            intent=intent, reply=reply, query_requests=queries or [], tasks=tasks or []
        )

    def test_chat_and_query_do_not_merge_pending_task(self):
        pending = empty_candidate("TRANSPORT")
        pending.update(pickup_station_id="warehouse", dropoff_station_id="packing")
        for output in (
            self.envelope(),
            self.envelope(
                "QUERY", "", [dict(query_type="ROBOT_LOCATION", parameters={})]
            ),
        ):
            answer = validate_interaction(output, ["warehouse", "packing"], [], pending)
            self.assertIsNone(answer.task_answer)
        self.assertEqual(pending["pickup_station_id"], "warehouse")

    def test_unknown_tool_and_chat_with_actions_are_rejected(self):
        for output in (
            self.envelope(
                "QUERY", "", [dict(query_type="EXECUTE_SHELL", parameters={})]
            ),
            self.envelope(
                "QUERY",
                "",
                [dict(query_type="ROBOT_LOCATION", parameters={"command": "go"})],
            ),
            self.envelope(tasks=[dict(task_type="NAVIGATE", parameters={})]),
            self.envelope(reply=""),
        ):
            with self.subTest(output=output), self.assertRaises(SemanticError):
                validate_interaction(output, [], [])

    def test_teaching_answers_and_budget(self):
        messages = build_interaction_messages("你好", ["dock_west"], [], [], None)
        for index in range(1, len(messages) - 1, 2):
            sample = json.loads(messages[index]["content"])
            output = json.loads(messages[index + 1]["content"])
            validate_interaction(
                output,
                sample.get("allowed_station_ids", []),
                sample.get("allowed_object_ids", []),
                sample.get("partial_candidate"),
            )
        self.assertLess(sum(len(m["content"].encode("utf-8")) for m in messages), 13000)
        self.assertEqual(
            json.loads(messages[-1]["content"])["allowed_station_ids"], ["dock_west"]
        )

    def test_backend_uses_interaction_schema_and_returns_chat(self):
        backend = OllamaBackend()
        captured = []

        def respond(endpoint, payload):
            captured.append(payload)
            return dict(done=True, message=dict(content=json.dumps(self.envelope())))

        backend.post = respond
        result = backend.interact("你好", [], [])
        self.assertEqual(result.reply, "你好！")
        self.assertEqual(result.intent, "CHAT")
        self.assertIn("query_requests", captured[0]["format"]["properties"])


class RobotStateCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.cache = RobotStateCache(clock=lambda: self.now)

    def test_missing_and_stale_telemetry_never_fabricates_position(self):
        self.assertEqual(
            self.cache.query("ROBOT_LOCATION", 10**10)[1], "ROBOT_DATA_UNAVAILABLE"
        )
        self.cache.update(
            "ROBOT_LOCATION",
            dict(x=1, y=2, z=0, yaw=0, frame_id="odom"),
            10**9,
            "/odom",
        )
        self.assertEqual(
            self.cache.query("ROBOT_LOCATION", 10**10)[1], "ROBOT_DATA_STALE"
        )
        self.cache.update(
            "ROBOT_LOCATION",
            dict(x=1, y=2, z=0, yaw=0, frame_id="odom"),
            10**10,
            "/odom",
        )
        self.now += 3
        self.assertFalse(self.cache.query("ROBOT_LOCATION", 10**10)[0])

    def test_location_reports_measured_frame_and_timestamp(self):
        self.cache.update(
            "ROBOT_LOCATION",
            dict(x=1.25, y=-2.5, z=0, yaw=0, frame_id="odom"),
            10**10,
            "/custom/odom",
        )
        success, code, reply, evidence = self.cache.query("ROBOT_LOCATION", 10**10)
        self.assertTrue(success)
        self.assertEqual(code, "")
        for text in ("1.25", "-2.50", "odom", "10.000", "/custom/odom"):
            self.assertIn(text, reply)
        self.assertEqual(evidence["stamp_ns"], 10**10)

    def test_invalid_measurements_fail_closed(self):
        for data in (
            dict(x=float("nan"), y=0, z=0, yaw=0, frame_id="odom"),
            dict(x=0, y=0, z=0, yaw=0, frame_id=""),
        ):
            self.cache.update("ROBOT_LOCATION", data, 10**10, "/odom")
            self.assertFalse(self.cache.query("ROBOT_LOCATION", 10**10)[0])
        for value in (-1.0, float("nan"), 80.0):
            self.cache.update(
                "ROBOT_BATTERY", dict(percentage=value), 10**10, "/battery_state"
            )
            self.assertFalse(self.cache.query("ROBOT_BATTERY", 10**10)[0])

    def test_status_and_battery_use_telemetry(self):
        self.cache.update(
            "ROBOT_STATUS",
            dict(state=2, active_task_id="task-123"),
            10**10,
            "/robot/status",
        )
        self.assertIn("task-123", self.cache.query("ROBOT_STATUS", 10**10)[2])
        self.cache.update(
            "ROBOT_BATTERY", dict(percentage=0.63), 10**10, "/battery_state"
        )
        self.assertIn("63%", self.cache.query("ROBOT_BATTERY", 10**10)[2])


if __name__ == "__main__":
    unittest.main()
