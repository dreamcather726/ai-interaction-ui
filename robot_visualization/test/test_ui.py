"""离屏 Qt 交互测试；模拟的展示数据只用于测试，不进入生产界面。"""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QAbstractItemView, QScrollArea

    from robot_visualization.ui.paging import PagedText

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
        panel.table.item(0, 2).setText("station_a")
        panel.table.item(1, 2).setText("station_b")
        self.assertEqual(panel.table.rowCount(), 40)
        panel.table.selectRow(1)
        panel.move_step(-1)
        self.assertEqual(panel.steps()[0]["destination_station_id"], "station_b")
        panel.remove_step()
        self.assertEqual(panel.table.rowCount(), 39)
        self.assertFalse(panel.buttons["confirm"].isEnabled())

    def test_manual_actions_only_inherit_explicit_plan_parameters(self):
        panel = self.window.draft_panel
        panel.new()
        panel.add_blank("NAVIGATE")
        panel.table.item(0, 2).setText("station_a")
        panel.add_blank("PICK")
        self.assertEqual(panel.table.item(1, 2).text(), "station_a")
        self.assertEqual(panel.table.item(1, 1).text(), "")
        panel.table.item(1, 1).setText("box_1")
        panel.add_blank("NAVIGATE")
        panel.table.item(2, 2).setText("station_b")
        panel.add_blank("PLACE")
        self.assertEqual(panel.table.item(3, 2).text(), "station_b")
        self.assertEqual(panel.table.item(3, 1).text(), "box_1")

    def test_transport_candidate_is_added_as_four_editable_actions(self):
        panel = self.window.draft_panel
        panel.new()
        panel.set_candidate(
            dict(
                request_id="audio-transport",
                session_id="session",
                input_context_id="context",
                success=True,
                outcome=0,
                candidate=dict(
                    task_type=2,
                    object_id="box_1",
                    pickup_station_id="station_a",
                    dropoff_station_id="station_b",
                    return_required=False,
                ),
            )
        )
        panel.add_candidate()
        self.assertEqual(panel.table.rowCount(), 4)
        self.assertEqual(
            [
                panel.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                for row in range(4)
            ],
            ["NAVIGATE", "PICK", "NAVIGATE", "PLACE"],
        )
        self.assertEqual(
            [panel.table.item(row, 2).text() for row in range(4)],
            ["station_a", "station_a", "station_b", "station_b"],
        )
        panel.add_candidate()
        self.assertEqual(panel.table.rowCount(), 4)

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
            candidate=dict(
                task_type=3,
                steps=[dict(task_type=1, destination_station_id="station_a")],
            ),
        )
        panel.receive(dict(operation="save", success=True, draft=value))
        self.assertTrue(panel.buttons["confirm"].isEnabled())
        panel.table.item(0, 2).setText("station_b")
        self.assertFalse(panel.buttons["confirm"].isEnabled())
        received = []
        panel.command_requested.connect(
            lambda operation, body: received.append((operation, body))
        )
        panel.confirm()
        self.assertEqual(received, [])

    def test_double_click_material_edits_in_persistent_field(self):
        self.window.pages.setCurrentIndex(2)
        panel = self.window.draft_panel
        panel.new()
        panel.add_blank("PICK")
        self.app.processEvents()
        field = panel.object_input
        cell = panel.table.visualItemRect(panel.table.item(0, 1)).center()
        QTest.mouseDClick(panel.table.viewport(), Qt.MouseButton.LeftButton, pos=cell)
        QTest.keyClicks(field, "box_1")
        self.app.processEvents()
        self.assertEqual(panel.steps()[0]["object_id"], "box_1")
        self.assertIs(field, panel.object_input)
        self.assertEqual(
            panel.table.editTriggers(), QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.assertIsNone(panel.table.cellWidget(0, 1))
        panel.add_blank("NAVIGATE")
        panel.table.selectRow(0)
        panel.move_step(1)
        self.assertEqual(panel.steps()[1]["object_id"], "box_1")
        panel.remove_step()
        self.assertEqual(len(panel.steps()), 1)
        self.assertIs(field, panel.object_input)

    def test_paging_keeps_off_page_steps_and_deletes_visible_selection(self):
        self.window.pages.setCurrentIndex(2)
        panel = self.window.draft_panel
        panel.new()
        for index in range(40):
            panel.add_blank("NAVIGATE")
            panel.table.item(index, 2).setText(f"station_{index}")
        self.app.processEvents()
        self.assertGreater(panel.table.pager.pages, 1)
        panel.table.pager.go(0)
        self.app.processEvents()
        self.assertEqual(panel.table.currentRow(), 0)
        panel.table.pager.go(1)
        row = panel.table.currentRow()
        self.assertFalse(panel.table.isRowHidden(row))
        self.assertEqual(panel.steps()[39]["destination_station_id"], "station_39")
        removed = panel.steps()[row]["destination_station_id"]
        panel.remove_step()
        self.assertNotIn(removed, [s["destination_station_id"] for s in panel.steps()])
        self.assertEqual(len(panel.steps()), 39)

    def test_text_pages_preserve_original_text_without_scroll_area(self):
        self.assertEqual(self.window.pages.count(), 6)
        self.assertFalse(self.window.findChildren(QScrollArea))
        view = PagedText()
        view.resize(400, 180)
        view.show()
        try:
            value = "\n".join(f"第 {index} 行测试文字" for index in range(100))
            view.setPlainText(value)
            self.app.processEvents()
            first = view.body.text()
            self.assertGreater(view.pager.pages, 1)
            view.pager.go(1)
            self.assertNotEqual(first, view.body.text())
            self.assertEqual(view.toPlainText(), value)
        finally:
            view.close()


if __name__ == "__main__":
    unittest.main()
