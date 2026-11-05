"""Construct real ROS nodes with generated arguments; never load real models."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROS_AVAILABLE = importlib.util.find_spec("rclpy") is not None
if ROS_AVAILABLE:
    import rclpy
    from rclpy.node import Node
    from rclpy.parameter import Parameter

    from ai_edge.asr.asr_node import ASRNode
    from ai_edge.audio.audio_processing_node import AudioProcessingNode
    from ai_edge.semantic.parse_task_node import ParseTaskNode
    from ai_edge.task_drafts.node import TaskDraftNode
    from robot_visualization.config import DashboardConfig
    from robot_visualization.configuration import load_configuration
    from robot_visualization.runtime.supervisor import BackendSupervisor


@unittest.skipUnless(ROS_AVAILABLE, "requires ROS 2 and built interfaces")
class BackendParameterTests(unittest.TestCase):
    def test_configuration_reaches_real_node_parameters_and_backends(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            values = load_configuration(
                [],
                {
                    "data_root": folder,
                    "model": "custom:4b",
                    "model_path": "models/test-asr",
                    "catalog_path": "catalog.sqlite3",
                    "draft_database_path": "drafts.sqlite3",
                    "audio_root": "audio",
                    "asr_ncpu": 2,
                    "asr_language": "en",
                    "asr_recognition_timeout": 10.0,
                    "asr_timeout": 15.0,
                    "request_timeout": 10.0,
                    "parse_timeout": 25.0,
                    "temperature": 0.2,
                    "num_ctx": 8192,
                    "num_predict": 1024,
                },
            )
            rclpy.init(args=[])
            try:
                dashboard = Node(
                    "config_handoff_test",
                    parameter_overrides=[
                        Parameter(name, value=value) for name, value in values.items()
                    ],
                )
                try:
                    config = DashboardConfig.from_node(dashboard)
                finally:
                    dashboard.destroy_node()
            finally:
                rclpy.shutdown()
            plans = dict(BackendSupervisor(config).plans())
            for name, factory, expected in (
                (
                    "parse_task_node",
                    ParseTaskNode,
                    {
                        "model": "custom:4b",
                        "num_ctx": 8192,
                        "temperature": 0.2,
                        "catalog_path": str(root / "catalog.sqlite3"),
                    },
                ),
                (
                    "asr_node",
                    ASRNode,
                    {
                        "model_path": str(root / "models/test-asr"),
                        "ncpu": 2,
                        "language": "en",
                        "recognition_timeout": 10.0,
                        "audio_root": str(root / "audio"),
                    },
                ),
                (
                    "audio_processing_node",
                    AudioProcessingNode,
                    {
                        "asr_timeout": 15.0,
                        "parse_timeout": 25.0,
                        "audio_root": str(root / "audio"),
                    },
                ),
                (
                    "task_draft_node",
                    TaskDraftNode,
                    {
                        "database_path": str(root / "drafts.sqlite3"),
                        "catalog_path": str(root / "catalog.sqlite3"),
                    },
                ),
            ):
                with self.subTest(node=name):
                    command = plans[name]
                    rclpy.init(args=command[command.index("--ros-args") :])
                    node = None
                    try:
                        with patch(
                            "ai_edge.asr.asr_node.FunASRBackend.load", return_value=None
                        ):
                            node = factory()
                            if name == "asr_node":
                                node.loading.result(timeout=2)
                                self.assertEqual(node.backend.ncpu, 2)
                                self.assertEqual(node.backend.language, "en")
                            elif name == "parse_task_node":
                                self.assertEqual(node.backend.model, "custom:4b")
                            for key, value in expected.items():
                                self.assertEqual(node.get_parameter(key).value, value)
                    finally:
                        if node is not None:
                            if hasattr(node, "close"):
                                node.close()
                            elif hasattr(node, "pipeline"):
                                node.pipeline.close()
                            node.destroy_node()
                        rclpy.shutdown()


if __name__ == "__main__":
    unittest.main()
