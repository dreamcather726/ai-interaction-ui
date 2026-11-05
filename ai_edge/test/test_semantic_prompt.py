"""检查教学答案的协议、动作依赖和提示词预算，不代替真实模型评测。"""

import json
import unittest

from ai_edge.semantic.ollama_backend import OllamaBackend
from ai_edge.semantic.semantic_contract import empty_candidate, validate_candidate
from ai_edge.semantic.semantic_prompt import build_messages
from ai_edge.task_drafts.actions import validate_action_plan


class SemanticPromptTests(unittest.TestCase):
    def setUp(self):
        self.messages = build_messages(
            "去仓库入口",
            ["warehouse_entry"],
            [],
            [],
            None,
            {
                "stations": [
                    dict(id="warehouse_entry", name="仓库入口", aliases=[], enabled=True)
                ],
                "objects": [],
            },
        )

    def examples(self):
        for index in range(1, len(self.messages) - 1, 2):
            yield (
                json.loads(self.messages[index]["content"]),
                json.loads(self.messages[index + 1]["content"]),
            )

    def test_all_teaching_answers_obey_the_task_contract(self):
        for sample, candidate in self.examples():
            with self.subTest(text=sample["text"]):
                answer = validate_candidate(
                    candidate, sample["allowed_station_ids"], sample["allowed_object_ids"]
                )
                self.assertFalse(answer.unsupported)
                if not answer.missing_fields:
                    validate_action_plan(
                        candidate,
                        sample["allowed_station_ids"],
                        sample["allowed_object_ids"],
                    )

    def test_numbered_routes_preserve_objects_and_final_return(self):
        routes = [
            (sample, candidate)
            for sample, candidate in self.examples()
            if "station_3" in sample["allowed_station_ids"]
        ]
        self.assertEqual(len(routes), 2)
        for sample, candidate in routes:
            with self.subTest(text=sample["text"]):
                plan = validate_action_plan(
                    candidate, sample["allowed_station_ids"], sample["allowed_object_ids"]
                )
                picks = [step for step in plan["steps"] if step["task_type"] == "PICK"]
                self.assertEqual(
                    [(step["object_id"], step["pickup_station_id"]) for step in picks],
                    [("material_x", "station_1"), ("material_y", "station_2")],
                )
                self.assertEqual(plan["steps"][-1]["destination_station_id"], "station_1")
                y_places = [
                    step["dropoff_station_id"]
                    for step in plan["steps"]
                    if step["task_type"] == "PLACE" and step["object_id"] == "material_y"
                ]
                self.assertEqual(
                    y_places, ["station_3"] if "3号点放下" in sample["text"] else []
                )

    def test_examples_do_not_replace_current_entities_or_exhaust_budget(self):
        payload = json.loads(self.messages[-1]["content"])
        self.assertEqual(payload["allowed_station_ids"], ["warehouse_entry"])
        self.assertEqual(payload["configured_entities"]["stations"][0]["name"], "仓库入口")
        size = sum(len(message["content"].encode("utf-8")) for message in self.messages)
        self.assertLess(size, 14000, "为真实输入、历史和白名单保留预算")

    def test_backend_preserves_varied_station_counts_and_custom_ids(self):
        configured_ids = [
            "warehouse_entry", "loading_west", "inspection_zone", "dock_27",
            "lift_lobby", "charger_north", "packing_room",
        ]
        for count in (1, 2, 4, 7):
            with self.subTest(station_count=count):
                stations = configured_ids[:count]
                expected = empty_candidate("NAVIGATE")
                expected["destination_station_id"] = stations[-1]
                captured = []

                def respond(endpoint, payload):
                    captured.append(json.loads(payload["messages"][-1]["content"]))
                    return {"done": True, "message": {"content": json.dumps(expected)}}

                backend = OllamaBackend()
                backend.post = respond
                answer = backend.parse(f"导航到 {stations[-1]}", stations, [])
                self.assertEqual(captured[0]["allowed_station_ids"], stations)
                self.assertEqual(answer.candidate["destination_station_id"], stations[-1])
                self.assertFalse(answer.missing_fields)


if __name__ == "__main__":
    unittest.main()
