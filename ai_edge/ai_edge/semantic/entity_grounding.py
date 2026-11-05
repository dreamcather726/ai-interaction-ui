"""将模型摘录的原文实体映射为真实ID；未提及或未知的实体不会猜成第一项。"""

from copy import deepcopy
import re

from ai_edge.common.catalog import seed_catalog
from ai_edge.semantic.semantic_contract import ID_FIELDS


def normalized(value):
    return re.sub(r"\s+", "", value).casefold()


def entity_index(rows, kind):
    index = {}
    for row in rows:
        if not row["enabled"]:
            continue
        labels = {row["id"], row["name"], *row["aliases"]}
        # 兼容显式白名单、未配置名称时的常见ID缩写，不覆盖用户配置的名称。
        if row["name"] == row["id"]:
            suffix = row["id"].rsplit("_", 1)[-1]
            if kind == "stations":
                labels.update((suffix + "点", suffix + "号点", suffix + "工位"))
            else:
                labels.update((suffix + "物料", "物料" + suffix))
                if row["id"].startswith("box_"):
                    labels.add(suffix + "号箱子")
                    if suffix in "123456789" and len(suffix) == 1:
                        labels.add("一二三四五六七八九"[int(suffix) - 1] + "号箱子")
        # A点/B物料之类的口语后缀不改变显式配置的字母或数字别名。
        for label in tuple(labels):
            if re.fullmatch(r"[a-zA-Z]|\d+", label):
                if kind == "stations":
                    labels.update(
                        label + ending for ending in ("点", "号点", "工位", "号工位")
                    )
                else:
                    labels.update((label + "物料", "物料" + label))
        for label in labels:
            index.setdefault(normalized(label), set()).add(row["id"])
    return index


def ground_interaction(data, text, stations, objects, entities=None, partial=None):
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return data
    result = deepcopy(data)
    constrain_queries(result, text)
    snapshot = entities if entities is not None else seed_catalog(stations, objects)
    indexes = {
        kind: entity_index([row for row in snapshot[kind] if row["id"] in ids], kind)
        for kind, ids in (("stations", stations), ("objects", objects))
    }
    trusted = set()
    if partial:
        for step in [partial, *partial.get("steps", [])]:
            trusted.update(step[field] for field in ID_FIELDS if step.get(field))
    original = normalized(text)
    location, carrying = "", ""
    for task in result["tasks"]:
        if not isinstance(task, dict) or not isinstance(task.get("parameters"), dict):
            continue
        parameters = task["parameters"]
        raw = dict(parameters)
        for field in ID_FIELDS:
            label = parameters.get(field)
            if not isinstance(label, str) or not label:
                continue
            key = normalized(label)
            matches = indexes["objects" if field == "object_id" else "stations"].get(
                key, set()
            )
            grounded = next(iter(matches)) if len(matches) == 1 else ""
            # 只接受本轮原文出现的名称/别名，或显式待补充候选已经提供的ID。
            parameters[field] = (
                grounded if grounded and (key in original or label in trusted) else ""
            )
        kind = task.get("task_type")
        if (
            kind in ("PICK", "TRANSPORT")
            and not raw.get("pickup_station_id")
            and location
        ):
            parameters["pickup_station_id"] = location
        if kind == "PLACE":
            if not raw.get("dropoff_station_id") and location:
                parameters["dropoff_station_id"] = location
            if not raw.get("object_id") and carrying:
                parameters["object_id"] = carrying
        if kind == "NAVIGATE":
            location = parameters.get("destination_station_id", "")
        elif kind == "TRANSPORT":
            location, carrying = parameters.get("dropoff_station_id", ""), ""
        elif kind == "PICK":
            carrying = parameters.get("object_id", "")
        elif kind == "PLACE":
            carrying = ""
    return result


def constrain_queries(data, text):
    """服务请求只覆盖本轮明确询问的项目，避免小模型顺带查询全部状态。"""
    from ai_edge.semantic.interaction_contract import QUERY_TYPES

    queries = data.get("query_requests")
    if not queries or any(
        not isinstance(q, dict)
        or set(q) != {"query_type", "parameters"}
        or q["query_type"] not in QUERY_TYPES
        or q["parameters"] != {}
        for q in queries
    ):
        return
    pattern = re.compile(
        r"(?P<ROBOT_LOCATION>位置|坐标|定位|在哪里|在哪儿|在哪|什么地方|何处)"
        r"|(?P<ROBOT_STATUS>状态|忙|在做什么|在干什么|运行情况|故障)"
        r"|(?P<ROBOT_BATTERY>电量|剩多少电|还剩多少电|剩余电)"
    )
    requested, mentioned = [], False
    for clause in re.split(r"[，,；;。！？?!、]", text):
        previous, negated = 0, False
        for match in pattern.finditer(clause):
            mentioned = True
            prefix = clause[previous : match.start()]
            if re.search(r"不要|不用|无需|不必|不需要|别|不是", prefix):
                negated = True
            elif re.search(r"查询|查|问|告诉|了解", prefix):
                negated = False
            if not negated and match.lastgroup not in requested:
                requested.append(match.lastgroup)
            previous = match.end()
    if mentioned:
        data["query_requests"] = [
            dict(query_type=kind, parameters={}) for kind in requested
        ]
        if requested:
            data["intent"], data["reply"] = "QUERY", ""
        elif data.get("tasks"):
            data["intent"] = "TASK"
        else:
            data.update(intent="CHAT", reply="好的，本轮不执行这些查询。")
