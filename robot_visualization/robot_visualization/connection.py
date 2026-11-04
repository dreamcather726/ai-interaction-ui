"""Deployment contract shared by the real and simulated robot clients."""

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class RobotConnection:
    prepare_service: str = "/task/prepare"
    execute_action: str = "/task/execute"
    query_service: str = "/task/query"
    cancel_service: str = "/task/cancel"
    snapshot_topic: str = "/task/snapshot"
    status_topic: str = "/robot/status"
    heartbeat_topic: str = "/robot/heartbeat"
    material_topic: str = "/robot/material_state"
    map_topic: str = "/map"
    camera_topic: str = "/camera/image_raw"
    odom_topic: str = "/odom"
    battery_topic: str = "/battery_state"
    executor_node: str = "/execute_task_action_server"
    response_timeout: float = 5.0
    heartbeat_timeout: float = 5.0
    cancel_timeout: float = 5.0
    telemetry_timeout: float = 2.0
    journal_path: str = "task_data/dashboard_dispatch.json"

    @classmethod
    def from_node(cls, node):
        defaults = cls(
            journal_path=(
                "task_data/simulation_dispatch.json"
                if node.get_parameter("use_simulator").value
                else "task_data/real_dispatch.json"
            )
        )
        values = {}
        for field in fields(cls):
            name = "robot_" + field.name
            node.declare_parameter(name, getattr(defaults, field.name))
            value = node.get_parameter(name).value
            if field.name.endswith("timeout"):
                if not 0.1 <= value <= 300:
                    raise ValueError(f"{name} must be in [0.1, 300] seconds")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
            values[field.name] = value
        return cls(**values)
