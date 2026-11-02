"""纯字典与 ROS 候选的转换；步骤顺序保持不变。"""

from robot_interfaces.msg import TaskStep
from ai_edge.semantic.semantic_contract import ID_FIELDS, NAMES


def assign_candidate(target, data, stamp):
    target.stamp = stamp
    target.task_type = {value: key for key, value in NAMES.items()}[data["task_type"]]
    for field in (*ID_FIELDS, "return_required"):
        setattr(target, field, data[field])
    if hasattr(target, "steps"):
        values = []
        for item in data.get("steps", []):
            step = TaskStep()
            assign_candidate(step, item, stamp)
            values.append(step)
        target.steps = values
