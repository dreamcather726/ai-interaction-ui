"""已加载 ROS 工作空间时验证真实订阅状态变化不会反复刷屏。"""

import importlib.util
import time
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

ROS_AVAILABLE = importlib.util.find_spec("rclpy") is not None
if ROS_AVAILABLE:
    import rclpy
    from robot_interfaces.msg import AudioProcessingState, ComponentStatus
    from robot_visualization.ros.node import DashboardNode


@unittest.skipUnless(ROS_AVAILABLE, "需要加载 ROS 2 与构建后的接口")
class ConsoleStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rclpy.init(args=[])

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        # 日志测试隔离真实资料库，不在用户的 task_data 下创建或读取配置。
        from ai_edge.common.catalog import CatalogStore

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        catalog = CatalogStore(Path(folder.name) / "catalog.sqlite3")
        override = patch(
            "robot_visualization.config.CatalogStore", return_value=catalog
        )
        override.start()
        self.addCleanup(override.stop)
        self.events = SimpleNamespace(
            report_log=Mock(),
            record=Mock(),
            availability=Mock(),
            control=Mock(),
            draft=Mock(),
        )
        self.node = DashboardNode(self.events)

    def tearDown(self):
        self.node.close()
        self.node.destroy_node()

    def test_simulated_scene_accepts_only_selected_microphone_health(self):
        self.node.config = replace(self.node.config, use_simulator=True)
        self.assertFalse(self.node.config.use_fake_microphone)
        status = ComponentStatus()
        status.component_id = "microphone"
        status.state = status.READY
        status.source_node = "/fake_microphone_sender"
        self.node.on_component(status)
        self.assertNotIn("microphone", self.node.components)
        status.source_node = "/microphone_sender_node"
        self.node.on_component(status)
        self.assertIn("microphone", self.node.components)
        self.node.components.clear()
        self.node.config = replace(
            self.node.config, simulator_audio_file="/tmp/command.wav"
        )
        self.assertTrue(self.node.config.use_fake_microphone)
        self.node.on_component(status)
        self.assertNotIn("microphone", self.node.components)
        status.source_node = "/fake_microphone_sender"
        self.node.on_component(status)
        self.assertIn("microphone", self.node.components)

    def test_poll_logs_only_changes_and_reports_lost_component(self):
        now = time.monotonic()
        self.node.publish_availability(now)
        self.events.report_log.reset_mock()
        self.node.publish_availability(now)
        self.events.report_log.assert_not_called()

        status = ComponentStatus()
        status.component_id = "asr"
        status.state = status.FAULT
        status.error_code = "ASR_DEPENDENCY_MISSING"
        status.message = "dependency probe"
        self.node.on_component(status)
        self.node.publish_availability(now)
        messages = [args[0] for args, _ in self.events.report_log.call_args_list]
        self.assertTrue(any("ASR_DEPENDENCY_MISSING" in text for text in messages))
        self.events.report_log.reset_mock()
        self.node.publish_availability(now)
        self.events.report_log.assert_not_called()

        self.node.publish_availability(now + 4)
        self.events.report_log.assert_called()
        self.events.report_log.reset_mock()
        self.node.publish_availability(now + 4)
        self.events.report_log.assert_not_called()

    def test_replayed_stage_does_not_duplicate_console_messages(self):
        message = AudioProcessingState()
        message.source_node = "/audio_processing_node"
        message.boot_id = "logging-probe"
        message.request_id = "request-one"
        message.event_seq = 1
        message.stage = message.RECOGNIZING
        self.node.on_state(message)
        self.node.on_state(message)
        self.events.report_log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
