"""工作台启动参数；不在界面中写入虚构工位或任务数据。"""

import sys
import shutil
import os
from dataclasses import dataclass, field, asdict, fields, replace
from pathlib import Path

from ai_edge.asr.model_paths import default_model_path
from ai_edge.common.catalog import CatalogStore, catalog_ids
from ai_edge.common.inference_config import (
    LLM_DEFAULTS,
    ASR_DEFAULTS,
    PIPELINE_DEFAULTS,
    bounded_number,
    validate_pipeline,
)
from robot_visualization.connection import RobotConnection


@dataclass(frozen=True)
class DashboardConfig:
    audio_root: Path
    model_path: str
    asr_python: str
    model: str
    ollama_url: str
    device: str
    stations: list
    objects: list
    start_backend: bool = True
    start_microphone: bool = True
    max_recording_seconds: float = 120.0
    catalog_path: Path = Path("task_data/catalog.sqlite3")
    catalog_snapshot: dict = field(default_factory=dict)
    use_simulator: bool = False
    use_sim_time: bool = False
    simulator_audio_file: str = ""
    robot: RobotConnection = field(default_factory=RobotConnection)
    data_root: Path = Path(".")
    draft_database_path: Path = Path("task_data/task_drafts.sqlite3")
    config_sources: str = "standalone ROS parameters"
    request_timeout: float = LLM_DEFAULTS["request_timeout"]
    temperature: float = LLM_DEFAULTS["temperature"]
    num_ctx: int = LLM_DEFAULTS["num_ctx"]
    num_predict: int = LLM_DEFAULTS["num_predict"]
    keep_alive: str = LLM_DEFAULTS["keep_alive"]
    robot_query_timeout: float = LLM_DEFAULTS["robot_query_timeout"]
    asr_device: str = ASR_DEFAULTS["asr_device"]
    asr_language: str = ASR_DEFAULTS["asr_language"]
    asr_ncpu: int = ASR_DEFAULTS["asr_ncpu"]
    asr_recognition_timeout: float = ASR_DEFAULTS["asr_recognition_timeout"]
    asr_timeout: float = PIPELINE_DEFAULTS["asr_timeout"]
    parse_timeout: float = PIPELINE_DEFAULTS["parse_timeout"]
    clarification_timeout: float = PIPELINE_DEFAULTS["clarification_timeout"]

    def effective_settings(self):
        """Deployment diagnostics without entity records or conversation contents."""
        value = asdict(self)
        for key in ("stations", "objects", "catalog_snapshot"):
            value.pop(key)
        value["deployment_mode"] = "simulation" if self.use_simulator else "real"
        return value

    @property
    def use_fake_microphone(self):
        return self.use_simulator and bool(self.simulator_audio_file)

    @classmethod
    def from_node(cls, node):
        from rclpy.parameter import Parameter

        model = default_model_path()
        python = Path.home() / "venvs/ros2_audio/bin/python"
        defaults = {
            # 从工作空间根目录启动时，与 src/build/install/log 同级。
            # 与独立节点的默认值一致；下方仅为跨进程文件交接解析绝对路径。
            "audio_root": "received_audio",
            "model_path": str(model),
            "asr_python": str(python) if python.is_file() else sys.executable,
            **LLM_DEFAULTS,
            **ASR_DEFAULTS,
            **PIPELINE_DEFAULTS,
            "data_root": ".",
            "draft_database_path": "task_data/task_drafts.sqlite3",
            "config_sources": "standalone ROS parameters",
            "device": "@DEFAULT_SOURCE@",
            "start_backend": True,
            "start_microphone": True,
            "max_recording_seconds": 120.0,
            "catalog_path": "task_data/catalog.sqlite3",
            "use_simulator": False,
            "simulator_audio_file": "",
        }
        for name, value in defaults.items():
            node.declare_parameter(name, value)
        for name in ("allowed_station_ids", "allowed_object_ids"):
            node.declare_parameter(name, Parameter.Type.STRING_ARRAY)
        values = {name: node.get_parameter(name).value for name in defaults}
        values = validate_parameters(values)
        for name in (
            "audio_root",
            "catalog_path",
            "draft_database_path",
            "data_root",
            "model_path",
            "asr_python",
            "device",
        ):
            if not values[name].strip():
                raise ValueError(f"{name} must not be empty")
        data_root = Path(values["data_root"]).expanduser().resolve()

        def absolute(value):
            path = Path(value).expanduser()
            return (
                path.resolve() if path.is_absolute() else (data_root / path).resolve()
            )

        asr_python = values["asr_python"]
        asr_python = (
            str(absolute(asr_python))
            if "/" in asr_python or "\\" in asr_python
            else shutil.which(asr_python)
        )
        if (
            not asr_python
            or not Path(asr_python).is_file()
            or not os.access(asr_python, os.X_OK)
        ):
            raise ValueError("asr_python executable was not found")
        robot = RobotConnection.from_node(node)
        robot = replace(robot, journal_path=str(absolute(robot.journal_path)))
        catalog = CatalogStore(absolute(values["catalog_path"]))
        snapshot = catalog.initialize(
            node.get_parameter_or("allowed_station_ids").value or [],
            node.get_parameter_or("allowed_object_ids").value or [],
        )
        stations, objects = catalog_ids(snapshot)
        return cls(
            audio_root=absolute(values["audio_root"]),
            model_path=str(absolute(values["model_path"])),
            asr_python=asr_python,
            model=node.get_parameter("model").value,
            ollama_url=node.get_parameter("ollama_url").value,
            device=node.get_parameter("device").value,
            stations=stations,
            objects=objects,
            start_backend=node.get_parameter("start_backend").value,
            start_microphone=node.get_parameter("start_microphone").value,
            max_recording_seconds=node.get_parameter("max_recording_seconds").value,
            catalog_path=catalog.path,
            catalog_snapshot=snapshot,
            use_simulator=node.get_parameter("use_simulator").value,
            use_sim_time=node.get_parameter("use_sim_time").value,
            simulator_audio_file=(
                str(absolute(values["simulator_audio_file"]))
                if values["simulator_audio_file"]
                else ""
            ),
            robot=robot,
            data_root=data_root,
            draft_database_path=absolute(values["draft_database_path"]),
            config_sources=values["config_sources"],
            **{
                name: values[name]
                for name in (*LLM_DEFAULTS, *ASR_DEFAULTS, *PIPELINE_DEFAULTS)
                if name not in ("model", "ollama_url")
            },
        )


def parameter_names():
    """Names accepted in deployment files; catch misspellings before starting ROS."""
    internal = {"stations", "objects", "catalog_snapshot", "robot", "config_sources"}
    return (
        {item.name for item in fields(DashboardConfig)} - internal
        | {"allowed_station_ids", "allowed_object_ids", "use_sim_time"}
        | {"robot_" + item.name for item in fields(RobotConnection)}
    )


def parameter_types():
    """ROS scalar types, available without importing ROS or constructing a node."""
    types = {
        item.name: str if item.type is Path else item.type
        for item in fields(DashboardConfig)
        if item.name in parameter_names()
    }
    types.update({"robot_" + item.name: item.type for item in fields(RobotConnection)})
    types.update(allowed_station_ids=list, allowed_object_ids=list)
    return types


def validate_parameters(values, *, partial=False):
    """Reject misspellings and wrong types before ROS launch or database writes."""
    types = parameter_types()
    unknown = set(values) - types.keys() - {"config_sources"}
    if unknown:
        raise ValueError(f"unknown dashboard parameters: {sorted(unknown, key=str)}")
    normalized = dict(values)
    for name, value in values.items():
        kind = types.get(name, str)
        if kind is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            bounded_number(name, value, -1e308, 1e308)
            normalized[name] = float(value)
        elif type(value) is not kind:
            raise ValueError(f"{name} must be {kind.__name__}")
        if kind is list and any(not isinstance(item, str) for item in value):
            raise ValueError(f"{name} must contain only strings")
        if kind is str and name != "simulator_audio_file" and not value.strip():
            raise ValueError(f"{name} must not be empty")
        if name.startswith("robot_") and name.endswith("timeout"):
            bounded_number(name, value, 0.1, 300)
    if not partial:
        effective = dict(LLM_DEFAULTS, **ASR_DEFAULTS, **PIPELINE_DEFAULTS)
        effective.update(normalized)
        validate_pipeline(effective)
        bounded_number(
            "max_recording_seconds",
            effective.get("max_recording_seconds", 120.0),
            1,
            300,
        )
    return normalized
