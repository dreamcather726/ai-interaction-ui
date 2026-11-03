"""将搬运意图展开为可编辑动作；静态校验不表示机器人已到达或已抓取。"""

from ai_edge.common import error_codes as Codes

from ai_edge.semantic.semantic_contract import (
    MAX_PLAN_STEPS,
    SemanticError,
    empty_candidate,
    validate_candidate,
)


def action(kind, **fields):
    value = empty_candidate(kind)
    value.pop("steps")
    value.update(fields)
    return value


def action_steps(candidate):
    """保留原顺序，将每次搬运展开；已拆开的动作不重复展开。"""
    source = candidate["steps"] if candidate["task_type"] == "SEQUENCE" else [candidate]
    result = []
    for step in source:
        kind = step["task_type"]
        if kind == "TRANSPORT":
            result.extend(
                [
                    action(
                        "NAVIGATE", destination_station_id=step["pickup_station_id"]
                    ),
                    action(
                        "PICK",
                        object_id=step["object_id"],
                        pickup_station_id=step["pickup_station_id"],
                    ),
                    action(
                        "NAVIGATE", destination_station_id=step["dropoff_station_id"]
                    ),
                    action(
                        "PLACE",
                        object_id=step["object_id"],
                        dropoff_station_id=step["dropoff_station_id"],
                    ),
                ]
            )
        elif kind == "NAVIGATE":
            result.append(
                action(
                    "NAVIGATE", destination_station_id=step["destination_station_id"]
                )
            )
        elif kind in ("PICK", "PLACE"):
            field = "pickup_station_id" if kind == "PICK" else "dropoff_station_id"
            result.append(
                action(
                    kind,
                    object_id=step["object_id"],
                    **{field: step[field]},
                )
            )
        else:
            raise SemanticError(Codes.UNSUPPORTED_TASK, "此候选不能展开为动作")
        if step["return_required"]:
            result.append(
                action("NAVIGATE", destination_station_id=step["return_station_id"])
            )
        if len(result) > MAX_PLAN_STEPS:
            raise SemanticError(
                Codes.PLAN_TOO_LONG, "展开后超过256个动作，未截断，请拆分草稿"
            )
    return result


def validate_action_plan(candidate, stations, objects):
    """服务端重新校验全部参数，再检查动作间的静态依赖。"""
    answer = validate_candidate(candidate, stations, objects)
    if answer.unsupported or answer.missing_fields:
        raise SemanticError(
            Codes.MISSING_FIELDS, answer.clarification_question or "动作参数不完整"
        )
    steps = action_steps(answer.candidate)
    # 草稿没有机器人的实时位置／抓持状态，只检查计划内已知信息的矛盾。
    unknown = object()
    location, carrying = None, unknown
    for index, step in enumerate(steps, 1):
        kind = step["task_type"]
        if kind == "NAVIGATE":
            location = step["destination_station_id"]
        elif kind == "PICK":
            if carrying not in (unknown, None) or (
                location is not None and location != step["pickup_station_id"]
            ):
                raise SemanticError(
                    Codes.INVALID_ACTION_ORDER,
                    f"第{index}步抓取与之前的导航目标或抓持动作冲突",
                )
            carrying = step["object_id"]
        elif carrying is unknown:
            raise SemanticError(
                Codes.INVALID_ACTION_ORDER,
                f"第{index}步没有先抓取物料就放下；草稿不能假设机器人已持有物料",
            )
        elif (carrying != step["object_id"]) or (
            location is not None and location != step["dropoff_station_id"]
        ):
            raise SemanticError(
                Codes.INVALID_ACTION_ORDER, f"第{index}步放下与之前的物料或导航目标冲突"
            )
        else:
            carrying = None
    return empty_candidate("SEQUENCE", steps)
