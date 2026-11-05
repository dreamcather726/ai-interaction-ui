"""实际Qt控件的对话发送、语音回复、去重与草稿隔离。"""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from robot_visualization.ui.window import DashboardWindow

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "需要PySide6")
class ConversationWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = DashboardWindow(
            SimpleNamespace(
                model="qwen3.5:2b",
                stations=[],
                objects=[],
                audio_root=Path("/tmp/audio"),
            )
        )
        self.window.show()
        self.window.receive_availability(dict(connected=True, conversation=True))
        self.app.processEvents()

    def tearDown(self):
        self.window.can_close = True
        self.window.close()
        self.app.processEvents()

    def test_text_sends_once_and_reply_is_visible_without_task_candidate(self):
        sent = []
        self.window.text_requested.connect(
            lambda request_id, text: sent.append((request_id, text))
        )
        self.window.chat_input.setText("你好")
        QTest.mouseClick(self.window.chat_send_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.window.chat_send_button, Qt.MouseButton.LeftButton)
        self.assertEqual(len(sent), 1)
        request_id, text = sent[0]
        self.window.receive_record(
            "parse",
            dict(
                request_id=request_id,
                intent="CHAT",
                reply="你好！有什么想聊的？",
                success=False,
                outcome=5,
            ),
        )
        self.assertIn(
            "你好！有什么想聊的？", self.window.conversation_text.toPlainText()
        )
        self.assertIn("你：你好", self.window.conversation_text.toPlainText())
        self.assertEqual(self.window.candidate_text.toPlainText(), "")
        self.assertFalse(self.window.home_add_candidate_button.isEnabled())
        self.assertTrue(self.window.chat_send_button.isEnabled())

    def test_voice_query_reply_opens_conversation_and_deduplicates_snapshot(self):
        self.window.receive_record("asr", dict(request_id="voice", text="你在哪里？"))
        response = dict(
            request_id="voice",
            intent="QUERY",
            reply="当前位置x=1.25米，坐标系odom。",
            success=False,
            outcome=5,
        )
        self.window.receive_record("parse", response)
        self.window.receive_record("parse", response)
        self.assertEqual(
            self.window.command_pages.currentIndex(),
            self.window.conversation_page_index,
        )
        self.assertEqual(
            self.window.conversation_text.toPlainText().count(response["reply"]), 1
        )
        self.assertFalse(self.window.home_add_candidate_button.isEnabled())

    def test_service_failure_is_visible_and_releases_send_button(self):
        sent = []
        self.window.text_requested.connect(
            lambda request_id, text: sent.append(request_id)
        )
        self.window.send_text("查询电量")
        self.window.receive_record(
            "parse",
            dict(
                request_id=sent[0],
                success=False,
                outcome=3,
                reply="对话服务尚未连接",
                error_code="DEPENDENCY_UNAVAILABLE",
            ),
        )
        self.assertIn("对话服务尚未连接", self.window.conversation_text.toPlainText())
        self.assertTrue(self.window.chat_send_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
