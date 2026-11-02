"""界面侧任务展示转换，不创建 ROS 或 Qt 对象。"""

from ai_edge.semantic.semantic_contract import ID_FIELDS, NAMES


def normalize_candidate(value, nested=False):
    kind = value.get("task_type", 0)
    data = dict(
        task_type=NAMES.get(kind, kind),
        **{field: value.get(field, "") for field in ID_FIELDS},
        return_required=value.get("return_required", False),
    )
    if not nested:
        data["steps"] = [
            normalize_candidate(step, True) for step in value.get("steps", [])
        ]
    return data
