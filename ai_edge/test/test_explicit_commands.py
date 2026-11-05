"""明确指令编排、任意实体映射及拒绝部分解析的回归。"""

import unittest

from ai_edge.common.catalog import seed_catalog
from ai_edge.semantic.entity_grounding import ground_interaction
from ai_edge.semantic.explicit_commands import (
    compile_explicit_commands,
    fill_explicit_answer,
)
from live_command_scenarios import STATIONS, OBJECTS, SCENARIOS, check_result


class ExplicitCommandTests(unittest.TestCase):
    def setUp(self):
        self.entities = {
            kind: [
                dict(id=identity, name=name, aliases=aliases, enabled=True)
                for identity, name, aliases in rows
            ]
            for kind, rows in (("stations", STATIONS), ("objects", OBJECTS))
        }
        self.stations = [row[0] for row in STATIONS]
        self.objects = [row[0] for row in OBJECTS]

    def test_explicit_scenarios_match_expected_order_and_clarification(self):
        for case in SCENARIOS:
            if case["intent"] == "QUERY":
                continue
            with self.subTest(name=case["name"]):
                answer = compile_explicit_commands(
                    case["text"], self.stations, self.objects, self.entities
                )
                self.assertIsNotNone(answer)
                check_result(case, answer, self.stations, self.objects)

    def test_does_not_accept_a_recognized_prefix_and_silently_drop_tail(self):
        for text in (
            "去原料库，然后给管理员发消息",
            "你会不会去原料库？",
            "不要去原料库",
        ):
            self.assertIsNone(
                compile_explicit_commands(
                    text, self.stations, self.objects, self.entities
                )
            )

    def test_numeric_ids_and_overlapping_letter_aliases_are_unambiguous_with_suffixes(
        self,
    ):
        entities = seed_catalog(["1", "2", "3"], ["x", "B"])
        for row, alias in zip(entities["stations"], ["A", "B", "C"]):
            row["aliases"] = [alias]
        answer = compile_explicit_commands(
            "去A点拿X物料，到B点放下，再拿起B物料送到C点放下，最后回1号点。",
            ["1", "2", "3"],
            ["x", "B"],
            entities,
        )
        self.assertIsNotNone(answer)
        steps = answer.task_answer.candidate["steps"]
        self.assertEqual(steps[0]["pickup_station_id"], "1")
        self.assertEqual(steps[0]["object_id"], "x")
        self.assertEqual(steps[1]["object_id"], "B")
        self.assertEqual(steps[1]["dropoff_station_id"], "3")
        self.assertEqual(steps[-1]["destination_station_id"], "1")

    def test_names_and_ids_do_not_need_numbering_or_ab_names(self):
        entities = seed_catalog(
            ["p_z91", "dock_42", "q_7", "lift_n", "bay_extra"], ["crate_q"]
        )
        entities["stations"][0].update(name="南侧门厅")
        entities["stations"][4].update(name="货架七层")
        entities["objects"][0].update(name="橙色工具箱")
        answer = compile_explicit_commands(
            "把橙色工具箱从南侧门厅送到货架七层放下，最后回南侧门厅。",
            [row["id"] for row in entities["stations"]],
            ["crate_q"],
            entities,
        )
        self.assertEqual(
            answer.task_answer.candidate["steps"][0]["pickup_station_id"], "p_z91"
        )
        self.assertEqual(
            answer.task_answer.candidate["steps"][0]["dropoff_station_id"], "bay_extra"
        )

    def test_single_entity_answer_fills_only_current_missing_parameter(self):
        pending = compile_explicit_commands(
            "去原料库取一个物料，到包装线放下，最后回东侧充电站。",
            self.stations,
            self.objects,
            self.entities,
        )
        answer = fill_explicit_answer(
            "X物料",
            pending.task_answer.candidate,
            self.stations,
            self.objects,
            self.entities,
        )
        self.assertFalse(answer.task_answer.missing_fields)
        self.assertEqual(
            answer.task_answer.candidate["steps"][0]["object_id"], "material_x"
        )
        self.assertEqual(pending.task_answer.candidate["steps"][0]["object_id"], "")
        self.assertIsNone(
            fill_explicit_answer(
                "你好",
                pending.task_answer.candidate,
                self.stations,
                self.objects,
                self.entities,
            )
        )

    def test_model_entity_not_in_original_or_not_configured_stays_missing(self):
        data = dict(
            intent="TASK",
            reply="",
            query_requests=[],
            tasks=[
                dict(
                    task_type="TRANSPORT",
                    parameters=dict(
                        object_id="X物料",
                        pickup_station_id="月球基地",
                        dropoff_station_id="包装线",
                    ),
                )
            ],
        )
        grounded = ground_interaction(
            data,
            "从月球基地取X物料送到包装线",
            self.stations,
            self.objects,
            self.entities,
        )
        self.assertEqual(grounded["tasks"][0]["parameters"]["pickup_station_id"], "")
        data["tasks"][0]["parameters"]["pickup_station_id"] = "原料库"
        grounded = ground_interaction(
            data,
            "从月球基地取X物料送到包装线",
            self.stations,
            self.objects,
            self.entities,
        )
        self.assertEqual(grounded["tasks"][0]["parameters"]["pickup_station_id"], "")

    def test_query_scope_does_not_include_unasked_or_negated_categories(self):
        data = dict(
            intent="QUERY",
            reply="",
            tasks=[],
            query_requests=[
                dict(query_type=kind, parameters={})
                for kind in ("ROBOT_LOCATION", "ROBOT_STATUS", "ROBOT_BATTERY")
            ],
        )
        for text, expected in (
            ("你现在在哪里？", ["ROBOT_LOCATION"]),
            ("机器人现在是什么状态？", ["ROBOT_STATUS"]),
            ("只查电量，不要查位置和状态", ["ROBOT_BATTERY"]),
            ("不用管位置和状态，只问电量", ["ROBOT_BATTERY"]),
            ("不要查询位置", []),
        ):
            with self.subTest(text=text):
                grounded = ground_interaction(
                    data, text, self.stations, self.objects, self.entities
                )
                self.assertEqual(
                    [query["query_type"] for query in grounded["query_requests"]],
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
