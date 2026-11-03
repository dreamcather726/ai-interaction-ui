"""任务候选的独立校验：单任务或可变长度的有序任务列表。"""

from ai_edge.common import error_codes as Codes

from dataclasses import dataclass


class SemanticError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


ID_FIELDS = (
    "object_id",
    "pickup_station_id",
    "dropoff_station_id",
    "destination_station_id",
    "return_station_id",
)
MAX_PLAN_STEPS = 256
STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "task_type": {
            "type": "string",
            "enum": ["NAVIGATE", "TRANSPORT", "PICK", "PLACE"],
        },
        **{field: {"type": "string"} for field in ID_FIELDS},
        "return_required": {"type": "boolean"},
    },
    "required": ["task_type", *ID_FIELDS, "return_required"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        **STEP_SCHEMA["properties"],
        "task_type": {
            "type": "string",
            "enum": ["UNKNOWN", "NAVIGATE", "TRANSPORT", "SEQUENCE", "PICK", "PLACE"],
        },
        "steps": {"type": "array", "maxItems": MAX_PLAN_STEPS, "items": STEP_SCHEMA},
    },
    "required": [*STEP_SCHEMA["required"], "steps"],
    "additionalProperties": False,
}
NAMES = {
    0: "UNKNOWN",
    1: "NAVIGATE",
    2: "TRANSPORT",
    3: "SEQUENCE",
    4: "PICK",
    5: "PLACE",
}


def candidate_payload(source):
    data = {
        "task_type": NAMES.get(source.task_type, "INVALID"),
        **{field: getattr(source, field) for field in ID_FIELDS},
        "return_required": source.return_required,
    }
    if hasattr(source, "steps"):
        data["steps"] = [candidate_payload(step) for step in source.steps]
    return data


def empty_candidate(kind="UNKNOWN", steps=None):
    return dict(
        task_type=kind,
        **{field: "" for field in ID_FIELDS},
        return_required=False,
        steps=list(steps or []),
    )


@dataclass(frozen=True)
class SemanticAnswer:
    candidate: dict
    missing_fields: tuple = ()
    clarification_question: str = ""
    unsupported: bool = False


def _primitive(data, stations, objects):
    if not isinstance(data, dict) or set(data) != set(STEP_SCHEMA["required"]):
        raise SemanticError(Codes.INVALID_MODEL_OUTPUT, "任务字段不完整或含额外字段")
    kinds = ("UNKNOWN", "NAVIGATE", "TRANSPORT", "PICK", "PLACE")
    if data["task_type"] not in kinds:
        raise SemanticError(Codes.INVALID_MODEL_OUTPUT, "不支持的任务类型")
    if type(data["return_required"]) is not bool or any(
        not isinstance(data[f], str) or len(data[f]) > 128 for f in ID_FIELDS
    ):
        raise SemanticError(Codes.INVALID_MODEL_OUTPUT, "任务字段类型或长度不符合约定")
    candidate = dict(data)
    for field in ID_FIELDS:
        candidate[field] = candidate[field].strip()
    task = candidate["task_type"]
    if task == "UNKNOWN":
        if candidate["return_required"] or any(candidate[f] for f in ID_FIELDS):
            raise SemanticError(
                Codes.INVALID_MODEL_OUTPUT, "不支持的任务不能携带运动参数"
            )
        return SemanticAnswer(candidate, unsupported=True)
    for field in ID_FIELDS:
        allowed = objects if field == "object_id" else stations
        if candidate[field] and candidate[field] not in allowed:
            raise SemanticError(
                Codes.UNKNOWN_OBJECT if field == "object_id" else Codes.UNKNOWN_STATION,
                f"字段 {field} 的标识不在白名单中：{candidate[field]}",
            )
    if task == "NAVIGATE":
        required = ["destination_station_id"]
        forbidden = ("object_id", "pickup_station_id", "dropoff_station_id")
    elif task == "TRANSPORT":
        required = ["object_id", "pickup_station_id", "dropoff_station_id"]
        forbidden = ("destination_station_id",)
    elif task == "PICK":
        required = ["object_id", "pickup_station_id"]
        forbidden = (
            "dropoff_station_id",
            "destination_station_id",
            "return_station_id",
        )
    else:
        required = ["object_id", "dropoff_station_id"]
        forbidden = ("pickup_station_id", "destination_station_id", "return_station_id")
    if task in ("PICK", "PLACE") and candidate["return_required"]:
        raise SemanticError(Codes.INVALID_MODEL_OUTPUT, "抓取和放下不能附带返回导航")
    if any(candidate[f] for f in forbidden) or (
        not candidate["return_required"] and candidate["return_station_id"]
    ):
        raise SemanticError(Codes.INVALID_MODEL_OUTPUT, "任务包含互斥字段")
    if candidate["return_required"]:
        required.append("return_station_id")
    missing = tuple(field for field in required if not candidate[field])
    labels = dict(
        object_id="物料编号",
        pickup_station_id="取货工位",
        dropoff_station_id="送货工位",
        destination_station_id="目标工位",
        return_station_id="返回工位",
    )
    question = (
        "请补充" + "、".join(labels[f] for f in missing) + "。" if missing else ""
    )
    if missing == ("object_id",):
        question = {"PICK": "要抓取哪个物料？", "PLACE": "要放下哪个物料？"}.get(
            task, "要搬运哪个物料？"
        )
    return SemanticAnswer(candidate, missing, question)


def validate_candidate(data, stations, objects):
    if not isinstance(data, dict):
        raise SemanticError(Codes.INVALID_MODEL_OUTPUT, "任务必须为对象")
    data = dict(data)
    data.setdefault("steps", [])
    if set(data) != set(SCHEMA["required"]) or not isinstance(data["steps"], list):
        raise SemanticError(Codes.INVALID_MODEL_OUTPUT, "任务结构不符合约定")
    steps = data.pop("steps")
    if data["task_type"] != "SEQUENCE":
        if steps:
            raise SemanticError(
                Codes.INVALID_MODEL_OUTPUT, "单任务不能同时携带步骤列表"
            )
        answer = _primitive(data, stations, objects)
        return SemanticAnswer(
            dict(answer.candidate, steps=[]),
            answer.missing_fields,
            answer.clarification_question,
            answer.unsupported,
        )
    _primitive(dict(data, task_type="UNKNOWN"), stations, objects)
    if not 1 <= len(steps) <= MAX_PLAN_STEPS:
        raise SemanticError(
            Codes.PLAN_TOO_LONG, f"步骤数量须为 1～{MAX_PLAN_STEPS}，不会截断任务"
        )
    checked, missing, question = [], [], ""
    for index, step in enumerate(steps):
        answer = _primitive(step, stations, objects)
        if answer.unsupported:
            raise SemanticError(
                Codes.INVALID_MODEL_OUTPUT, "列表中的步骤必须是导航、抓取、放下或搬运"
            )
        checked.append(answer.candidate)
        missing.extend(f"steps[{index}].{field}" for field in answer.missing_fields)
        if answer.missing_fields and not question:
            question = f"第 {index + 1} 步：{answer.clarification_question}"
    return SemanticAnswer(
        empty_candidate("SEQUENCE", checked), tuple(missing), question
    )


def reconcile_candidate(data, partial):
    if not partial or data["task_type"] == "UNKNOWN":
        return data
    if data["task_type"] != partial["task_type"] or (
        data["return_required"] != partial["return_required"]
    ):
        raise SemanticError(
            Codes.DIALOGUE_CONFLICT, "任务类型或返回要求已改变，请先说“重新开始”"
        )
    merged = dict(data)
    for field in ID_FIELDS:
        previous, current = partial[field], data[field].strip()
        if previous and current and previous != current:
            raise SemanticError(
                Codes.DIALOGUE_CONFLICT, f"已有 {field} 冲突，请先说“重新开始”"
            )
        merged[field] = previous or current
    if data["task_type"] == "SEQUENCE":
        if len(data["steps"]) != len(partial["steps"]):
            raise SemanticError(
                Codes.DIALOGUE_CONFLICT, "澄清期间不能增删或重排步骤，请先重新开始"
            )
        merged["steps"] = [
            reconcile_candidate(new, old)
            for new, old in zip(data["steps"], partial["steps"])
        ]
    return merged
