"""工作台启动参数；不在界面中写入虚构工位或任务数据。"""

import sys
from dataclasses import dataclass
from pathlib import Path

from rclpy.parameter import Parameter

from ai_edge.asr.model_paths import default_model_path


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

    @classmethod
    def from_node(cls, node):
        model = default_model_path()
        python = Path.home() / "venvs/ros2_audio/bin/python"
        defaults = {
            # 从工作空间根目录启动时，与 src/build/install/log 同级。
            # 与独立节点的默认值一致；下方仅为跨进程文件交接解析绝对路径。
            "audio_root": "received_audio",
            "model_path": str(model),
            "asr_python": str(python) if python.is_file() else sys.executable,
            "model": "qwen3.5:2b",
            "ollama_url": "http://127.0.0.1:11434",
            "device": "@DEFAULT_SOURCE@",
            "start_backend": True,
            "start_microphone": True,
            "max_recording_seconds": 120.0,
        }
        for name, value in defaults.items():
            node.declare_parameter(name, value)
        for name in ("allowed_station_ids", "allowed_object_ids"):
            node.declare_parameter(name, Parameter.Type.STRING_ARRAY)
        if not 1 <= node.get_parameter("max_recording_seconds").value <= 300:
            raise ValueError("max_recording_seconds 必须在 1～300 秒之间")
        return cls(
            audio_root=Path(node.get_parameter("audio_root").value)
            .expanduser()
            .resolve(),
            model_path=node.get_parameter("model_path").value,
            asr_python=str(Path(node.get_parameter("asr_python").value).expanduser()),
            model=node.get_parameter("model").value,
            ollama_url=node.get_parameter("ollama_url").value,
            device=node.get_parameter("device").value,
            stations=node.get_parameter_or("allowed_station_ids").value or [],
            objects=node.get_parameter_or("allowed_object_ids").value or [],
            start_backend=node.get_parameter("start_backend").value,
            start_microphone=node.get_parameter("start_microphone").value,
            max_recording_seconds=node.get_parameter("max_recording_seconds").value,
        )
