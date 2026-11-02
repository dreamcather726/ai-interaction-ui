"""模型输出的独立业务校验；只生成候选，不授予执行权限。"""

from dataclasses import dataclass


class SemanticError(ValueError):
    """通过稳定错误码向 ROS 层报告失败。"""

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
SCHEMA = {
    "type": "object",
    "properties": {
        "task_type": {"type": "string", "enum": ["UNKNOWN", "NAVIGATE", "TRANSPORT"]},
        **{field: {"type": "string"} for field in ID_FIELDS},
        "return_required": {"type": "boolean"},
    },
    "required": ["task_type", *ID_FIELDS, "return_required"],
    "additionalProperties": False,
}


def candidate_payload(source):
    """将 ROS 候选转换成模型和规则层使用的普通字典。"""
    names = {0: "UNKNOWN", 1: "NAVIGATE", 2: "TRANSPORT"}
    return {
        "task_type": names.get(source.task_type, "INVALID"),
        **{field: getattr(source, field) for field in ID_FIELDS},
        "return_required": source.return_required,
    }


def reconcile_candidate(data, partial):
    """续轮保留已知字段，只允许补缺；修改已有目标须显式重开任务。"""
    if not partial or data["task_type"] == "UNKNOWN":
        return data
    if data["task_type"] != partial["task_type"] or (
        data["return_required"] != partial["return_required"]
    ):
        raise SemanticError(
            "DIALOGUE_CONFLICT", "补充内容改变了原任务类型或返回要求，请先说“重新开始”"
        )
    merged = dict(data)
    for field in ID_FIELDS:
        previous, current = partial[field], data[field].strip()
        if previous and current and previous != current:
            raise SemanticError(
                "DIALOGUE_CONFLICT", f"补充内容与已有 {field} 冲突，请先说“重新开始”"
            )
        merged[field] = previous or current
    return merged


@dataclass(frozen=True)
class SemanticAnswer:
    candidate: dict
    missing_fields: tuple = ()
    clarification_question: str = ""
    unsupported: bool = False


def validate_candidate(data, stations, objects):
    """不信任 JSON schema 的强制效果，再检查类型、白名单和互斥字段。"""
    if not isinstance(data, dict) or set(data) != set(SCHEMA["required"]):
        raise SemanticError("INVALID_MODEL_OUTPUT", "模型输出字段不完整或含额外字段")
    if data["task_type"] not in ("UNKNOWN", "NAVIGATE", "TRANSPORT"):
        raise SemanticError("INVALID_MODEL_OUTPUT", "模型返回了不支持的任务类型")
    if type(data["return_required"]) is not bool or any(
        not isinstance(data[f], str) or len(data[f]) > 128 for f in ID_FIELDS
    ):
        raise SemanticError("INVALID_MODEL_OUTPUT", "模型字段类型或标识长度不符合约定")
    candidate = dict(data)
    for field in ID_FIELDS:
        candidate[field] = candidate[field].strip()
    task = candidate["task_type"]
    if task == "UNKNOWN":
        if candidate["return_required"] or any(candidate[f] for f in ID_FIELDS):
            raise SemanticError("INVALID_MODEL_OUTPUT", "不支持的任务不能携带运动参数")
        return SemanticAnswer(candidate, unsupported=True)
    for field in ID_FIELDS:
        value = candidate[field]
        allowed = objects if field == "object_id" else stations
        if value and value not in allowed:
            code = "UNKNOWN_OBJECT" if field == "object_id" else "UNKNOWN_STATION"
            raise SemanticError(code, f"字段 {field} 的标识不在本轮白名单中：{value}")
    if task == "NAVIGATE":
        required = ["destination_station_id"]
        forbidden = ("object_id", "pickup_station_id", "dropoff_station_id")
    else:
        required = ["object_id", "pickup_station_id", "dropoff_station_id"]
        forbidden = ("destination_station_id",)
    if any(candidate[f] for f in forbidden) or (
        not candidate["return_required"] and candidate["return_station_id"]
    ):
        raise SemanticError("INVALID_MODEL_OUTPUT", "模型返回了互斥的任务字段")
    if candidate["return_required"]:
        required.append("return_station_id")
    missing = tuple(field for field in required if not candidate[field])
    labels = {
        "object_id": "物料编号",
        "pickup_station_id": "取货工位",
        "dropoff_station_id": "送货工位",
        "destination_station_id": "目标工位",
        "return_station_id": "返回工位",
    }
    question = (
        "请补充" + "、".join(labels[f] for f in missing) + "。" if missing else ""
    )
    if missing == ("object_id",):
        question = "要搬运哪个物料？"
    return SemanticAnswer(candidate, missing, question)
