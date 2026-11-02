"""工作台启动参数；不在界面中写入虚构工位或任务数据。"""

import sys
from dataclasses import dataclass
from pathlib import Path

from rclpy.parameter import Parameter


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

    @classmethod
    def from_node(cls, node):
        model = (
            Path.home()
            / ".cache/modelscope/models/iic--SenseVoiceSmall/snapshots/master"
        )
        python = Path.home() / "venvs/ros2_audio/bin/python"
        defaults = {
            "audio_root": str(Path.home() / "ai_interaction_ws/received_audio"),
            "model_path": str(model) if model.is_dir() else "iic/SenseVoiceSmall",
            "asr_python": str(python) if python.is_file() else sys.executable,
            "model": "qwen3.5:2b",
            "ollama_url": "http://127.0.0.1:11434",
            "device": "@DEFAULT_SOURCE@",
            "start_backend": True,
            "start_microphone": True,
        }
        for name, value in defaults.items():
            node.declare_parameter(name, value)
        for name in ("allowed_station_ids", "allowed_object_ids"):
            node.declare_parameter(name, Parameter.Type.STRING_ARRAY)
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
        )
