"""用未完成 Future 模拟超时和迟到结果；不启动模型或 ROS 节点。"""

import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from ai_edge.asr.asr_node import ASRNode
    from ai_edge.semantic.parse_task_node import ParseTaskNode

    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False


@unittest.skipUnless(ROS_AVAILABLE, "需要已构建的 ROS 接口")
class InferenceTimeoutTests(unittest.TestCase):
    def job(self):
        return dict(
            worker=Future(),
            response=Future(),
            deadline=1.0,
            discard=False,
            handle=SimpleNamespace(is_cancel_requested=False),
        )

    def test_asr_timeout_keeps_slot_then_discards_late_result(self):
        job = self.job()
        node = SimpleNamespace(
            pending=job, publish_health=Mock(), get_logger=lambda: Mock()
        )
        with patch("ai_edge.asr.asr_node.time.monotonic", return_value=2.0):
            ASRNode._poll_recognition(node)
            self.assertEqual(job["response"].exception().code, "ASR_TIMEOUT")
            self.assertTrue(job["discard"])
            self.assertIs(node.pending, job)
            job["worker"].set_result("late transcript")
            ASRNode._poll_recognition(node)
        self.assertIsNone(node.pending)
        self.assertEqual(job["response"].exception().code, "ASR_TIMEOUT")

    def test_parse_timeout_does_not_publish_late_candidate(self):
        job = self.job()
        node = SimpleNamespace(pending=job, get_logger=lambda: Mock())
        with patch("ai_edge.semantic.parse_task_node.time.monotonic", return_value=2.0):
            ParseTaskNode.poll(node)
            self.assertEqual(job["response"].exception().code, "PARSE_TIMEOUT")
            self.assertTrue(job["discard"])
            job["worker"].set_result("late candidate")
            ParseTaskNode.poll(node)
        self.assertIsNone(node.pending)
        self.assertEqual(job["response"].exception().code, "PARSE_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
