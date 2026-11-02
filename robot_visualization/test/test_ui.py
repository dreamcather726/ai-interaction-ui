"""离屏 Qt 交互测试；模拟的展示数据只用于测试，不进入生产界面。"""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from robot_visualization.ui.window import DashboardWindow

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "需要 PySide6")
class WindowTests(unittest.TestCase):
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
        self.app.processEvents()
        self.value = dict(
            services={},
            start=True,
            stop=True,
            asr_ready=True,
            recording=False,
            query=True,
            connected=True,
        )

    def tearDown(self):
        self.window.can_close = True
        self.window.close()

    def test_initial_screen_does_not_invent_results(self):
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertEqual(self.window.asr_text.toPlainText(), "")
        self.assertEqual(self.window.history.count(), 0)

    def test_duplicate_click_does_not_send_two_starts(self):
        sent = []
        self.window.start_requested.connect(lambda: sent.append("start"))
        self.window.receive_availability(self.value)
        QTest.mouseClick(self.window.start_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.window.start_button, Qt.MouseButton.LeftButton)
        self.assertEqual(sent, ["start"])

    def test_timer_cannot_assume_capture_stopped(self):
        self.window.receive_availability(dict(self.value, recording=True))
        self.window.recording_since -= 12
        self.window.tick()
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())

    def test_missing_device_state_never_enables_start(self):
        self.window.receive_availability(dict(self.value, recording=None))
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())

    def test_selecting_history_keeps_results_with_their_request(self):
        self.window.receive_record("asr", {"request_id": "one", "text": "第一段"})
        self.window.receive_record("asr", {"request_id": "two", "text": "第二段"})
        self.window.history.setCurrentRow(1)
        self.assertEqual(self.window.asr_text.toPlainText(), "第一段")
        self.window.receive_record(
            "asr",
            {
                "request_id": "two",
                "text": "第二段更新",
                "stamp_ns": 1,
            },
        )
        self.assertEqual(self.window.asr_text.toPlainText(), "第一段")

    def test_failed_parse_never_displays_an_executable_candidate(self):
        self.window.receive_record(
            "parse",
            {
                "request_id": "one",
                "success": True,
                "outcome": 0,
                "candidate": {"task_type": 1, "destination_station_id": "station_a"},
            },
        )
        self.assertIn("station_a", self.window.candidate_text.toPlainText())
        self.window.receive_record(
            "parse",
            {
                "request_id": "two",
                "success": False,
                "outcome": 3,
                "candidate": {"task_type": 1, "destination_station_id": "station_b"},
                "error_code": "LLM_UNAVAILABLE",
            },
        )
        self.assertEqual(self.window.candidate_text.toPlainText(), "")
        self.assertIn("LLM_UNAVAILABLE", self.window.result_message.text())

    def test_query_only_requests_existing_record_by_id(self):
        sent = []
        self.window.query_requested.connect(sent.append)
        self.window.receive_availability(self.value)
        self.window.query_input.setText("  audio-request-one  ")
        QTest.mouseClick(self.window.query_button, Qt.MouseButton.LeftButton)
        self.assertEqual(sent, ["audio-request-one"])
        self.assertEqual(self.window.history.count(), 0)

    def test_close_waits_for_worker_and_requests_shutdown_once(self):
        sent = []
        self.window.shutdown_requested.connect(lambda: sent.append("shutdown"))
        self.window.close()
        self.window.close()
        self.assertTrue(self.window.isVisible())
        self.assertEqual(sent, ["shutdown"])
        self.window.on_worker_finished()
        self.app.processEvents()
        self.assertFalse(self.window.isVisible())

    def test_draft_rows_have_variable_length_and_keep_order(self):
        panel = self.window.draft_panel
        panel.new()
        for _ in range(40):
            panel.add_blank("NAVIGATE")
        panel.table.item(0, 4).setText("station_a")
        panel.table.item(1, 4).setText("station_b")
        self.assertEqual(panel.table.rowCount(), 40)
        panel.table.selectRow(1)
        panel.move_step(-1)
        self.assertEqual(panel.steps()[0]["destination_station_id"], "station_b")
        panel.remove_step()
        self.assertEqual(panel.table.rowCount(), 39)
        self.assertFalse(panel.buttons["confirm"].isEnabled())

    def test_editing_saved_draft_disables_confirmation(self):
        panel = self.window.draft_panel
        panel.set_online(True)
        value = dict(
            draft_id="draft-one",
            revision=1,
            event_seq=1,
            boot_id="boot-one",
            state=0,
            session_id="session",
            input_context_id="context",
            source_request_ids=["audio-one"],
            confirmation_token="token-one",
            summary="导航到 station_a",
            candidate=dict(task_type=1, destination_station_id="station_a"),
        )
        panel.receive(dict(operation="save", success=True, draft=value))
        self.assertTrue(panel.buttons["confirm"].isEnabled())
        panel.table.item(0, 4).setText("station_b")
        self.assertFalse(panel.buttons["confirm"].isEnabled())
        received = []
        panel.command_requested.connect(
            lambda operation, body: received.append((operation, body))
        )
        panel.confirm()
        self.assertEqual(received, [])


if __name__ == "__main__":
    unittest.main()
