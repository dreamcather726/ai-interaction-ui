"""显式运行的真实模型复杂指令验收；使用临时资料库，不下发机器人执行。

python live_command_scenarios.py --report <output.json>
"""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_edge.common.catalog import CatalogStore, catalog_ids
from ai_edge.semantic.ollama_backend import OllamaBackend
from ai_edge.task_drafts.actions import validate_action_plan

STATIONS = [
    ("warehouse_in", "原料库", ["仓库入口", "1号点"]),
    ("inspection_area", "质检区", ["检测台", "2号点"]),
    ("packing_line", "包装线", ["打包区", "3号点"]),
    ("temporary_zone", "暂存区", ["中转区", "5号点"]),
    ("charger_east", "东侧充电站", ["充电区", "9号点"]),
]
OBJECTS = [
    ("material_x", "X物料", ["物料X"]),
    ("material_y", "Y物料", ["物料Y"]),
    ("box_blue", "蓝箱", ["蓝色周转箱", "蓝色箱子"]),
]


def nav(station):
    return ("NAVIGATE", station, "")


def pick(station, obj):
    return ("PICK", station, obj)


def place(station, obj):
    return ("PLACE", station, obj)


SCENARIOS = [
    dict(
        name="two_materials_and_return",
        intent="TASK",
        text="去1号点拿X物料，到2号点放下；再拿起Y物料送到3号点放下，最后空车返回1号点。",
        expected_actions=[
            nav("warehouse_in"),
            pick("warehouse_in", "material_x"),
            nav("inspection_area"),
            place("inspection_area", "material_x"),
            pick("inspection_area", "material_y"),
            nav("packing_line"),
            place("packing_line", "material_y"),
            nav("warehouse_in"),
        ],
    ),
    dict(
        name="three_deliveries_five_stations",
        intent="TASK",
        text="把蓝色周转箱从暂存区送到包装线放下，再把X物料从原料库送到质检区放下，然后将Y物料从质检区送到暂存区放下，最后小车回东侧充电站。",
        expected_actions=[
            nav("temporary_zone"),
            pick("temporary_zone", "box_blue"),
            nav("packing_line"),
            place("packing_line", "box_blue"),
            nav("warehouse_in"),
            pick("warehouse_in", "material_x"),
            nav("inspection_area"),
            place("inspection_area", "material_x"),
            pick("inspection_area", "material_y"),
            nav("temporary_zone"),
            place("temporary_zone", "material_y"),
            nav("charger_east"),
        ],
    ),
    dict(
        name="explicit_primitive_actions",
        intent="TASK",
        text="导航到原料库，抓取X物料，导航到质检区，放下X物料，抓取Y物料，导航到包装线，放下Y物料，然后导航到东侧充电站。",
        expected_actions=[
            nav("warehouse_in"),
            pick("warehouse_in", "material_x"),
            nav("inspection_area"),
            place("inspection_area", "material_x"),
            pick("inspection_area", "material_y"),
            nav("packing_line"),
            place("packing_line", "material_y"),
            nav("charger_east"),
        ],
    ),
    dict(
        name="return_goods_then_empty_robot",
        intent="TASK",
        text="把蓝箱从暂存区送到包装线放下，再把同一个蓝箱从包装线送回暂存区放下，最后机器人去东侧充电站。",
        expected_actions=[
            nav("temporary_zone"),
            pick("temporary_zone", "box_blue"),
            nav("packing_line"),
            place("packing_line", "box_blue"),
            pick("packing_line", "box_blue"),
            nav("temporary_zone"),
            place("temporary_zone", "box_blue"),
            nav("charger_east"),
        ],
    ),
    dict(
        name="navigation_only_with_negation",
        intent="TASK",
        text="导航到质检区，再去包装线，最后回原料库；整个过程不要抓取或放下任何物料。",
        expected_actions=[
            nav("inspection_area"),
            nav("packing_line"),
            nav("warehouse_in"),
        ],
    ),
    dict(
        name="battery_query_excludes_location",
        intent="QUERY",
        text="只查当前电量，不要查询位置，也不要下发任务。",
        expected_queries=["ROBOT_BATTERY"],
    ),
    dict(
        name="status_query_only",
        intent="QUERY",
        text="机器人现在是什么状态？",
        expected_queries=["ROBOT_STATUS"],
    ),
    dict(
        name="location_query_only",
        intent="QUERY",
        text="你现在在哪里？",
        expected_queries=["ROBOT_LOCATION"],
    ),
    dict(
        name="ambiguous_dropoff_requires_question",
        intent="CLARIFY",
        text="把X物料从原料库送到质检区放下，再拿起Y物料去包装线，最后回原料库。",
    ),
    dict(
        name="missing_object_must_not_be_guessed",
        intent="CLARIFY",
        text="去原料库取一个物料，送到包装线放下，最后返回东侧充电站。",
        missing_object=True,
    ),
    dict(
        name="unconfigured_station_requires_question",
        intent="CLARIFY",
        text="去月球基地拿X物料送到包装线放下。",
        missing_station=True,
    ),
]


def canonical_actions(candidate, stations, objects):
    plan = validate_action_plan(candidate, stations, objects)
    result, location = [], None
    for step in plan["steps"]:
        kind = step["task_type"]
        if kind == "NAVIGATE":
            target = step["destination_station_id"]
            if target != location:
                result.append(nav(target))
            location = target
        else:
            field = "pickup_station_id" if kind == "PICK" else "dropoff_station_id"
            result.append((kind, step[field], step["object_id"]))
    return result


def check_result(scenario, result, stations, objects):
    answer = result.task_answer
    expected = scenario["intent"]
    if expected == "TASK":
        assert (
            result.intent == "TASK" and answer is not None and not answer.missing_fields
        ), "未生成完整任务"
        actual = canonical_actions(answer.candidate, stations, objects)
        assert actual == scenario["expected_actions"], f"动作不一致：{actual}"
    elif expected == "QUERY":
        assert result.intent == "QUERY" and answer is None
        assert (
            list(result.query_types) == scenario["expected_queries"]
        ), result.query_types
    else:
        needs_fields = answer is not None and bool(answer.missing_fields)
        assert (
            result.intent == "CLARIFY" or needs_fields
        ), "应追问，不能生成可执行完整任务"
        if scenario["name"] == "ambiguous_dropoff_requires_question":
            assert (
                result.intent == "CLARIFY" and "放下" in result.reply
            ), "没有追问是否放下Y"
        if answer is not None:
            steps = (
                answer.candidate["steps"]
                if answer.candidate["task_type"] == "SEQUENCE"
                else [answer.candidate]
            )
            transports = [step for step in steps if step["task_type"] == "TRANSPORT"]
            if scenario.get("missing_object"):
                assert (
                    len(transports) == 1 and not transports[0]["object_id"]
                ), "缺失物料被擅自补齐或新增搬运"
                assert transports[0]["pickup_station_id"] == "warehouse_in"
                assert transports[0]["dropoff_station_id"] == "packing_line"
                assert any(
                    field.endswith("object_id") for field in answer.missing_fields
                )
            if scenario.get("missing_station"):
                assert (
                    len(transports) == 1 and not transports[0]["pickup_station_id"]
                ), "未知工位被替换成已有工位"
                assert transports[0]["object_id"] == "material_x"
                assert transports[0]["dropoff_station_id"] == "packing_line"


def run(model="qwen3.5:2b", report_path=None, model_only=False):
    records = []
    with tempfile.TemporaryDirectory(prefix="complex-command-catalog-") as folder:
        catalog = CatalogStore(Path(folder) / "catalog.sqlite3")
        snapshot = catalog.initialize([], [])
        for kind, rows in (("stations", STATIONS), ("objects", OBJECTS)):
            for identity, name, aliases in rows:
                snapshot = catalog.update(
                    "save",
                    kind,
                    dict(id=identity, name=name, aliases=aliases, enabled=True),
                    snapshot["revision"],
                )
        stations, objects = catalog_ids(snapshot)
        backend = OllamaBackend(model=model, timeout=120, catalog=catalog)
        for scenario in SCENARIOS:
            record = dict(scenario, model=model)
            try:
                from ai_edge.semantic.explicit_commands import compile_explicit_commands

                record["planner"] = (
                    "explicit_commands"
                    if compile_explicit_commands(
                        scenario["text"], stations, objects, snapshot
                    )
                    is not None
                    else "model"
                )
                if model_only:
                    from ai_edge.semantic.interaction_contract import (
                        INTERACTION_SCHEMA,
                        validate_interaction,
                    )
                    from ai_edge.semantic.semantic_prompt import (
                        build_interaction_messages,
                    )
                    from ai_edge.semantic.entity_grounding import ground_interaction

                    record["planner"] = "model"
                    messages = build_interaction_messages(
                        scenario["text"], stations, objects, [], None, snapshot
                    )
                    raw = backend.generate(messages, INTERACTION_SCHEMA)
                    record["raw_model_output"] = raw
                    result = validate_interaction(
                        ground_interaction(
                            raw, scenario["text"], stations, objects, snapshot
                        ),
                        stations,
                        objects,
                    )
                else:
                    result = backend.interact(scenario["text"], stations, objects)
                record["actual"] = asdict(result)
                check_result(scenario, result, stations, objects)
                record["passed"] = True
            except Exception as exc:
                record.update(passed=False, error=str(exc))
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
    if report_path:
        Path(report_path).write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    passed = sum(record["passed"] for record in records)
    print(f"PASSED {passed}/{len(records)}", flush=True)
    return passed == len(records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3.5:2b")
    parser.add_argument("--report")
    parser.add_argument(
        "--model-only",
        action="store_true",
        help="只测模型抽取与实体映射，绕过明确指令编排规则",
    )
    args = parser.parse_args()
    raise SystemExit(0 if run(args.model, args.report, args.model_only) else 1)
