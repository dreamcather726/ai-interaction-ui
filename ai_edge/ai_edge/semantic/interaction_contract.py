"""交互模型的受限输出：回复、只读查询与已有类型的任务候选。"""

from dataclasses import dataclass

from ai_edge.common import error_codes as Codes
from ai_edge.semantic.semantic_contract import (
    ID_FIELDS,
    MAX_PLAN_STEPS,
    SemanticError,
    empty_candidate,
    reconcile_candidate,
    validate_candidate,
)

QUERY_TYPES = ("ROBOT_LOCATION", "ROBOT_STATUS", "ROBOT_BATTERY")
TASK_FIELDS = {
    "NAVIGATE": ("destination_station_id",),
    "TRANSPORT": ("object_id", "pickup_station_id", "dropoff_station_id"),
    "PICK": ("object_id", "pickup_station_id"),
    "PLACE": ("object_id", "dropoff_station_id"),
}
INTERACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["CHAT", "QUERY", "TASK", "CLARIFY", "UNSUPPORTED"],
        },
        "reply": {"type": "string", "maxLength": 4096},
        "query_requests": {
            "type": "array",
            "maxItems": len(QUERY_TYPES),
            "items": {
                "type": "object",
                "properties": {
                    "query_type": {"type": "string", "enum": list(QUERY_TYPES)},
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
                "required": ["query_type", "parameters"],
                "additionalProperties": False,
            },
        },
        "tasks": {
            "type": "array",
            "maxItems": MAX_PLAN_STEPS,
            "items": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "task_type": {"type": "string", "enum": [kind]},
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    field: {"type": "string"} for field in fields
                                },
                                "required": list(fields),
                                "additionalProperties": False,
                            },
                        },
                        "required": ["task_type", "parameters"],
                        "additionalProperties": False,
                    }
                    for kind, fields in TASK_FIELDS.items()
                ]
            },
        },
    },
    "required": ["intent", "reply", "query_requests", "tasks"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class InteractionAnswer:
    intent: str
    reply: str
    query_types: tuple = ()
    task_answer: object = None


def validate_interaction(data, stations, objects, partial=None):
    def invalid(message):
        raise SemanticError(Codes.INVALID_MODEL_OUTPUT, message)

    if not isinstance(data, dict) or set(data) != set(INTERACTION_SCHEMA["required"]):
        invalid("交互输出字段不完整或包含额外字段")
    intent, reply = data["intent"], data["reply"]
    if intent not in INTERACTION_SCHEMA["properties"]["intent"]["enum"]:
        invalid("未知交互意图")
    if not isinstance(reply, str) or len(reply) > 4096:
        invalid("回复内容类型或长度不正确")
    queries, tasks = data["query_requests"], data["tasks"]
    if not isinstance(queries, list) or not isinstance(tasks, list):
        invalid("查询和任务必须为列表")
    if len(queries) > len(QUERY_TYPES) or len(tasks) > MAX_PLAN_STEPS:
        invalid("查询或任务数量超出限制")
    query_types = []
    for query in queries:
        if (
            not isinstance(query, dict)
            or set(query) != {"query_type", "parameters"}
            or query["query_type"] not in QUERY_TYPES
            or query["parameters"] != {}
        ):
            invalid("不支持的查询类型或参数")
        if query["query_type"] in query_types:
            invalid("同一轮不能重复请求相同查询")
        query_types.append(query["query_type"])
    if intent in ("CHAT", "UNSUPPORTED") and (queries or tasks):
        invalid("闲聊或不支持的请求不能携带查询和动作")
    if intent == "QUERY" and not queries:
        invalid("查询意图缺少查询请求")
    if intent == "TASK" and (not tasks or queries):
        invalid("任务意图必须有任务，含查询时应标记为QUERY")
    if intent in ("CHAT", "CLARIFY", "UNSUPPORTED") and not reply.strip():
        if not (intent == "CLARIFY" and queries):
            invalid("该交互意图必须包含回复")
    if queries and not tasks and intent in ("QUERY", "CLARIFY"):
        # 查询所缺的实时数据由服务获取，不应反问用户“电量是多少”。
        intent, reply = "QUERY", ""
    checked = None
    if tasks:
        steps = []
        for task in tasks:
            if (
                not isinstance(task, dict)
                or set(task) != {"task_type", "parameters"}
                or not isinstance(task["parameters"], dict)
                or "task_type" in task["parameters"]
                or "steps" in task["parameters"]
            ):
                invalid("任务参数结构不符合约定")
            kind, parameters = task["task_type"], task["parameters"]
            if kind not in TASK_FIELDS or not set(TASK_FIELDS[kind]).issubset(
                parameters
            ):
                invalid("任务缺少对应动作的参数字段")
            if set(parameters) - {*ID_FIELDS, "return_required"}:
                invalid("任务包含未定义参数")
            step = empty_candidate(kind)
            step.pop("steps")
            step.update(parameters)
            steps.append(step)
        candidate = (
            dict(steps[0], steps=[])
            if len(steps) == 1
            else empty_candidate("SEQUENCE", steps)
        )
        checked = validate_candidate(candidate, stations, objects)
        if checked.unsupported:
            invalid("交互任务不能使用UNKNOWN动作")
        checked = validate_candidate(
            reconcile_candidate(checked.candidate, partial), stations, objects
        )
    return InteractionAnswer(intent, reply.strip(), tuple(query_types), checked)
