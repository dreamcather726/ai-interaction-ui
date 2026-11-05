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

    def test_voice_query_reply_preserves_tabs_and_deduplicates_snapshot(self):
        original_tab = self.window.command_pages.currentIndex()
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
            original_tab,
        )
        self.assertEqual(
            self.window.conversation_text.toPlainText().count(response["reply"]), 1
        )
        self.assertFalse(self.window.home_add_candidate_button.isEnabled())

    def test_only_current_round_is_displayed_and_late_old_reply_is_ignored(self):
        self.window.receive_record("asr", dict(request_id="old", text="旧问题"))
        self.window.receive_record(
            "parse", dict(request_id="old", intent="CHAT", reply="旧回答", outcome=5)
        )
        self.window.receive_record("asr", dict(request_id="new", text="新问题"))
        self.assertNotIn("旧", self.window.conversation_text.toPlainText())
        self.window.receive_record(
            "parse", dict(request_id="new", intent="CHAT", reply="新回答", outcome=5)
        )
        self.window.receive_record(
            "parse",
            dict(
                request_id="old",
                intent="CHAT",
                reply="迟到的旧回答",
                outcome=5,
                stamp_ns=1,
            ),
        )
        self.assertEqual(
            self.window.conversation_text.toPlainText(), "你：新问题\n\n助手：新回答"
        )

    def test_text_input_and_reply_do_not_switch_selected_tabs(self):
        self.window.command_pages.setCurrentIndex(0)
        self.window.input_tabs.setCurrentIndex(1)
        self.window.text_input.setText("去仓库")
        sent = []
        self.window.text_requested.connect(lambda rid, text: sent.append((rid, text)))
        QTest.mouseClick(self.window.text_send_button, Qt.MouseButton.LeftButton)
        self.assertEqual(sent[0][1], "去仓库")
        self.assertEqual(self.window.command_pages.currentIndex(), 0)
        self.assertEqual(self.window.input_tabs.currentIndex(), 1)
        self.window.receive_record(
            "parse",
            dict(
                request_id=sent[0][0], intent="CLARIFY", reply="请指定工位", outcome=5
            ),
        )
        self.assertEqual(self.window.command_pages.currentIndex(), 0)
        self.assertEqual(self.window.input_tabs.currentIndex(), 1)
        self.assertIn("请指定工位", self.window.input_reply.text())

    def test_current_reply_update_preserves_page_already_being_read(self):
        self.window.command_pages.setCurrentIndex(self.window.conversation_page_index)
        self.window.receive_record("asr", dict(request_id="long", text="详细介绍"))
        response = dict(
            request_id="long",
            intent="CHAT",
            reply="很长的本轮回复。\n" * 100,
            outcome=5,
        )
        self.window.receive_record("parse", response)
        self.app.processEvents()
        self.window.conversation_text._reflow()
        self.assertGreater(self.window.conversation_text.pager.pages, 1)
        self.window.conversation_text.pager.go(1)
        self.window.receive_record(
            "parse", dict(response, reply=response["reply"] + "补充说明", stamp_ns=1)
        )
        self.app.processEvents()
        self.assertEqual(self.window.conversation_text.pager.page, 1)

    def test_text_command_can_enter_existing_draft_flow(self):
        from ai_edge.common.catalog import seed_catalog
        from ai_edge.semantic.explicit_commands import compile_explicit_commands

        snapshot = seed_catalog(["1", "2", "3"], ["x", "B"])
        self.window.receive_catalog(dict(success=True, snapshot=snapshot))
        self.window.receive_availability(
            dict(connected=True, conversation=True, drafts=True)
        )
        text = "去1号点拿X物料，到2号点放下，再拿起B物料送到3号点放下，最后回1号点。"
        sent = []
        self.window.text_requested.connect(lambda rid, text: sent.append(rid))
        self.window.input_tabs.setCurrentIndex(1)
        self.window.text_input.setText(text)
        QTest.mouseClick(self.window.text_send_button, Qt.MouseButton.LeftButton)
        compiled = compile_explicit_commands(
            text, ["1", "2", "3"], ["x", "B"], snapshot
        )
        self.window.receive_record(
            "parse",
            dict(
                request_id=sent[0],
                intent="TASK",
                reply="任务候选已生成",
                success=True,
                outcome=0,
                candidate=compiled.task_answer.candidate,
            ),
        )
        self.assertTrue(self.window.home_add_candidate_button.isEnabled())
        self.assertIn("共 8 步", self.window.candidate_text.toPlainText())
        self.assertEqual(self.window.command_pages.currentIndex(), 0)
        self.assertEqual(self.window.input_tabs.currentIndex(), 1)

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
