"""对能完整匹配的明确动作语句作确定性编排；其余输入交给模型。

语法中的实体来自当前资料库，不预设点位名称、数量或编号。
整句中任何一个动作片段无法识别时不采用部分编排，避免漏掉后半句。
"""

import re
from copy import deepcopy

from ai_edge.common.catalog import seed_catalog
from ai_edge.semantic.entity_grounding import entity_index, normalized
from ai_edge.semantic.interaction_contract import InteractionAnswer
from ai_edge.semantic.semantic_contract import empty_candidate, validate_candidate
from ai_edge.task_drafts.actions import action, validate_action_plan


def fill_explicit_answer(text, partial, stations, objects, entities=None):
    """仅补充当前追问的缺项；一个明确的实体名称不必再次交给模型猜意图。"""
    checked = validate_candidate(partial, stations, objects)
    if not checked.missing_fields:
        return None
    field_path = checked.missing_fields[0]
    field = field_path.rsplit(".", 1)[-1]
    kind, ids = ("objects", objects) if field == "object_id" else ("stations", stations)
    snapshot = entities if entities is not None else seed_catalog(stations, objects)
    index = entity_index([row for row in snapshot[kind] if row["id"] in ids], kind)
    value = normalized(text).strip("。.!！?？ ")
    prefixes = (
        ("就是", "就用", "选择", "是", "用")
        if kind == "objects"
        else ("返回到", "返回", "回到", "选择", "到", "去", "回", "是")
    )
    if kind == "stations" and field not in (
        "destination_station_id",
        "return_station_id",
    ):
        prefixes = ("选择", "到", "去", "是")
    variants = [
        value,
        *(value[len(prefix) :] for prefix in prefixes if value.startswith(prefix)),
    ]
    matches = set().union(*(index.get(variant, set()) for variant in variants))
    if len(matches) != 1:
        return None
    candidate = deepcopy(partial)
    target = candidate
    step = re.fullmatch(r"steps\[(\d+)\]\.(.+)", field_path)
    if step:
        target = candidate["steps"][int(step[1])]
    target[field] = matches.pop()
    answer = validate_candidate(candidate, stations, objects)
    if not answer.missing_fields:
        validate_action_plan(answer.candidate, stations, objects)
    return InteractionAnswer("TASK", "", (), answer)


MOVE = r"(?:送回到|送回|送到|送往|运到|运回|搬运到|搬到|搬回|移动到|到)"
TAKE = r"(?:抓取|拿起|拿走|取走|拿|取|抓)"
DROP = r"(?:放下|放好|卸下|放置)"
S = r"(?:~s\d+~|[^~，,；;。]+?)"
O = r"(?:~o\d+~|一个物料|某个物料|东西|某件物料)"
PREFIX = r"^(?:(?:请|麻烦|我要|帮我|然后|接着|随后|再|最后|先|小车|机器人|空车|把|将|同一个))+(?:，)?"


def compile_explicit_commands(text, stations, objects, entities=None):
    if re.search(r"[?？]|能不能|会不会|是否|假如|如果|介绍", text):
        return None
    snapshot = entities if entities is not None else seed_catalog(stations, objects)
    labels, tokens, ambiguous = {}, {}, set()
    for kind, ids, prefix in (("stations", stations, "s"), ("objects", objects, "o")):
        rows = [row for row in snapshot[kind] if row["id"] in ids]
        for label, matches in entity_index(rows, kind).items():
            if len(matches) != 1:
                continue
            identity = next(iter(matches))
            token = f"~{prefix}{ids.index(identity)}~"
            if label in labels and labels[label] != token:
                ambiguous.add(label)
                del labels[label]
            if label not in ambiguous:
                labels[label] = token
            tokens[token] = identity
    if not labels:
        return None
    alternatives = []
    for label in sorted(labels, key=len, reverse=True):
        escaped = re.escape(label)
        if re.fullmatch(r"[a-z0-9_]+", label):
            escaped = r"(?<![a-z0-9_])" + escaped + r"(?![a-z0-9_])"
        alternatives.append(escaped)
    tokenized = re.sub(
        "|".join(alternatives), lambda match: labels[match.group()], normalized(text)
    )
    tokenized = tokenized.replace("同一个", "")
    clauses = [
        re.sub(PREFIX, "", clause)
        for clause in re.split(r"[，,；;。]+", tokenized)
        if clause
    ]
    if not any(token in tokenized for token in tokens):
        return None
    steps, location, carrying = [], "", ""
    carrying_known = False
    forbid_gripper = False

    def navigate(identity):
        nonlocal location
        steps.append(action("NAVIGATE", destination_station_id=identity))
        location = identity

    def transport(source, obj, target):
        nonlocal location, carrying, carrying_known
        steps.append(
            action(
                "TRANSPORT",
                object_id=obj,
                pickup_station_id=source,
                dropoff_station_id=target,
            )
        )
        location, carrying, carrying_known = target, "", False

    def match_transport(clause):
        patterns = (
            rf"(?P<obj>{O})从(?P<src>{S}){MOVE}(?P<dst>{S}){DROP}",
            rf"(?:去|到|前往)?(?P<src>{S}){TAKE}(?P<obj>{O}){MOVE}(?P<dst>{S}){DROP}",
            rf"从(?P<src>{S}){TAKE}(?P<obj>{O}){MOVE}(?P<dst>{S}){DROP}",
        )
        for pattern in patterns:
            found = re.fullmatch(pattern, clause)
            if found:
                return (
                    tokens.get(found["src"], ""),
                    tokens.get(found["obj"], ""),
                    tokens.get(found["dst"], ""),
                )
        found = re.fullmatch(rf"{TAKE}(?P<obj>{O}){MOVE}(?P<dst>{S}){DROP}", clause)
        if found:
            return location, tokens.get(found["obj"], ""), tokens.get(found["dst"], "")
        return None

    index = 0
    while index < len(clauses):
        clause = clauses[index]
        consumed = 1
        delivery = match_transport(clause)
        if delivery is None and index + 1 < len(clauses):
            delivery = match_transport(clause + clauses[index + 1])
            if delivery is not None:
                consumed = 2
        if delivery is not None:
            if carrying_known:
                return None
            transport(*delivery)
        else:
            nav_match = re.fullmatch(
                r"(?:导航到|前往|返回到|返回|回到|回|去|到)(?P<station>~s\d+~)", clause
            )
            pick_match = re.fullmatch(
                rf"(?:(?:导航到|去|到)(?P<station>~s\d+~))?{TAKE}(?P<obj>{O})(?:去(?P<target>~s\d+~))?",
                clause,
            )
            place_match = re.fullmatch(
                rf"(?:(?:导航到|送到|去|到)(?P<station>~s\d+~))?{DROP}(?P<obj>~o\d+~)?",
                clause,
            )
            if nav_match:
                navigate(tokens[nav_match["station"]])
            elif pick_match:
                if carrying_known:
                    return None
                if pick_match["station"]:
                    navigate(tokens[pick_match["station"]])
                carrying = tokens.get(pick_match["obj"], "")
                carrying_known = True
                steps.append(
                    action("PICK", object_id=carrying, pickup_station_id=location)
                )
                if pick_match["target"]:
                    navigate(tokens[pick_match["target"]])
            elif place_match:
                if not carrying_known:
                    return None
                if place_match["station"]:
                    navigate(tokens[place_match["station"]])
                explicit_obj = tokens.get(place_match["obj"], "")
                if explicit_obj and carrying and explicit_obj != carrying:
                    return None
                steps.append(
                    action(
                        "PLACE",
                        object_id=explicit_obj or carrying,
                        dropoff_station_id=location,
                    )
                )
                carrying, carrying_known = "", False
            elif re.fullmatch(
                r"(?:整个过程)?(?:不要|不许|禁止)(?:抓取|拿起|取|放下|或|和|任何物料)+",
                clause,
            ):
                forbid_gripper = True
            else:
                return None
        index += consumed
    if not steps or (
        forbid_gripper and any(step["task_type"] != "NAVIGATE" for step in steps)
    ):
        return None
    candidate = (
        dict(steps[0], steps=[])
        if len(steps) == 1
        else empty_candidate("SEQUENCE", steps)
    )
    checked = validate_candidate(candidate, stations, objects)
    if (
        carrying_known
        and steps[-1]["task_type"] == "NAVIGATE"
        and re.search(r"回|返回", text)
    ):
        return InteractionAnswer(
            "CLARIFY", "到目的地后，需要放下新抓取的物料再返回吗？"
        )
    if not checked.missing_fields:
        validate_action_plan(checked.candidate, stations, objects)
    return InteractionAnswer("TASK", "", (), checked)
