"""把 ROS Goal 转换为受限的纯 Python 输入，不调用模型。"""

from ai_edge.semantic_contract import ID_FIELDS, SemanticError, validate_candidate


def read_parse_goal(goal):
    """限制总输入大小，检查来源与续轮条件，保持调用方的上下文边界。"""
    for field in ("request_id", "session_id", "input_id", "input_context_id"):
        value = getattr(goal, field)
        if not value.strip() or len(value) > 256:
            raise SemanticError(
                "INVALID_REQUEST", f"{field} 不能为空且不能超过 256 字符"
            )
    if not goal.text.strip() or len(goal.text) > 4096:
        raise SemanticError("INVALID_REQUEST", "待解析文字不能为空且不能超过 4096 字符")
    if goal.stamp.sec < 0 or not 0 <= goal.stamp.nanosec < 10**9:
        raise SemanticError("INVALID_REQUEST", "请求时间戳不合法")
    stations, objects = list(goal.allowed_station_ids), list(goal.allowed_object_ids)
    for values in (stations, objects):
        if (
            len(values) > 128
            or len(set(values)) != len(values)
            or any(
                not value.strip() or value != value.strip() or len(value) > 128
                for value in values
            )
        ):
            raise SemanticError(
                "INVALID_REQUEST", "白名单不能含重复、空白标识或超长条目"
            )
    context = []
    for turn in goal.context:
        if turn.role not in (turn.USER, turn.ASSISTANT) or not turn.text.strip():
            raise SemanticError("INVALID_REQUEST", "历史对话角色或文字不合法")
        context.append(
            {
                "role": "user" if turn.role == turn.USER else "assistant",
                "text": turn.text,
            }
        )
    if (
        len(context) > 20
        or sum(len(t["text"]) for t in context) + len(goal.text) > 12000
    ):
        raise SemanticError("INVALID_REQUEST", "对话超过 20 条或总文字超过 12000 字符")
    names = {0: "UNKNOWN", 1: "NAVIGATE", 2: "TRANSPORT"}
    source = goal.partial_candidate
    partial = {field: getattr(source, field) for field in ID_FIELDS}
    partial.update(
        task_type=names.get(source.task_type, "INVALID"),
        return_required=source.return_required,
    )
    checked = validate_candidate(partial, stations, objects)
    if source.task_type != source.UNKNOWN and not context:
        raise SemanticError(
            "INVALID_REQUEST", "部分候选只能在显式提供同一上下文历史的澄清续轮使用"
        )
    return (
        goal.text,
        stations,
        objects,
        context,
        None if checked.unsupported else checked.candidate,
    )
