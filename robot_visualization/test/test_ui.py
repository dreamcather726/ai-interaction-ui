"""离屏 Qt 交互测试；模拟的展示数据只用于测试，不进入生产界面。"""

import os
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QApplication,
        QAbstractItemView,
        QScrollArea,
        QStackedWidget,
    )

    from robot_visualization.ui.paging import PagedText
    from ai_edge.common.catalog import seed_catalog

    from robot_visualization.ui.window import DashboardWindow

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "需要 PySide6")
class WindowTests(unittest.TestCase):
    def emit_odom(self, window, value):
        window.receive_record(
            "robot_odom",
            dict(
                value,
                frame_id="map",
                map_frame="map",
                map_valid=True,
                map_x=value["x"],
                map_y=value["y"],
            ),
        )

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
        self.assertFalse(self.window.home_add_candidate_button.isEnabled())
        self.assertFalse(self.window.home_confirm_draft_button.isEnabled())

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
        self.deliver_preflight(panel.saved)
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
        self.window.switch_page(self.window.page_indexes["任务管理"])
        panel = self.window.draft_panel
        panel.new()
        panel.add_blank("PICK")
        panel.set_catalog(seed_catalog([], ["box_1"]))
        self.app.processEvents()
        field = panel.object_input
        cell = panel.table.visualItemRect(panel.table.item(0, 1)).center()
        QTest.mouseDClick(panel.table.viewport(), Qt.MouseButton.LeftButton, pos=cell)
        field.box.setCurrentIndex(field.box.findData("box_1"))
        field.box.activated.emit(field.box.currentIndex())
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
        self.window.switch_page(self.window.page_indexes["任务管理"])
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
        self.assertIsInstance(self.window.pages, QStackedWidget)
        self.assertEqual(self.window.pages.count(), 7)
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

    def test_browsing_saved_draft_pages_does_not_select_or_clear_unsaved_plan(self):
        panel = self.window.draft_panel
        panel.new()
        panel.add_blank("NAVIGATE")
        panel.table.item(0, 2).setText("station_a")
        chosen = []
        panel.history.activated.connect(chosen.append)
        for index in range(12):
            panel.history.addItem(str(index), str(index))
        panel.history.setCurrentIndex(0)
        panel.history.pager.go(1)
        self.assertEqual(chosen, [])
        self.assertEqual(panel.steps()[0]["destination_station_id"], "station_a")
        self.assertTrue(panel.dirty)
        panel.history.box.activated.emit(0)
        self.assertEqual(chosen, [5])

    def test_catalog_picker_retains_disabled_id_without_silently_selecting_another(
        self,
    ):
        panel = self.window.draft_panel
        panel.new()
        panel.add_blank("PICK")
        panel.table.item(0, 1).setText("retired_box")
        panel.focus_field(0, 1)
        snapshot = seed_catalog(["station_a"], ["box_1"])
        panel.set_catalog(snapshot)
        self.assertEqual(panel.object_input.text(), "retired_box")
        self.assertEqual(panel.steps()[0]["object_id"], "retired_box")
        self.assertNotIn("retired_box", panel.objects)

    def test_catalog_save_is_async_and_updates_the_draft_whitelist_on_receipt(self):
        panel = self.window.catalog_panels["stations"]
        sent = []
        self.window.catalog_requested.connect(
            lambda operation, value: sent.append((operation, value))
        )
        panel.new()
        panel.identity.setText("loading_zone")
        panel.name.setText("装货区")
        panel.aliases.setText("取货区，装货点")
        panel.mark_dirty()
        panel.save()
        self.assertTrue(panel.pending)
        self.assertEqual(sent[0][0], "save")
        self.assertEqual(sent[0][1]["entity"]["aliases"], ["取货区", "装货点"])
        self.assertNotIn("loading_zone", self.window.draft_panel.stations)
        snapshot = seed_catalog(["loading_zone"], [])
        snapshot["revision"] = 1
        self.window.receive_catalog(
            dict(success=True, kind="stations", snapshot=snapshot)
        )
        self.assertFalse(panel.pending)
        self.assertIn("loading_zone", self.window.draft_panel.stations)

    def test_text_wrap_preserves_non_bmp_characters_and_whole_words_when_they_fit(self):
        view = PagedText()
        view.resize(220, 160)
        view.show()
        try:
            original = "你好🙂 alpha beta gamma delta " * 6
            view.setPlainText(original)
            self.app.processEvents()
            self.assertEqual("".join(view._lines), original)
            for word in ("alpha", "beta", "gamma", "delta"):
                self.assertEqual(sum(line.count(word) for line in view._lines), 6)
        finally:
            view.close()

    def test_navigation_keeps_draft_and_moves_one_existing_log_card(self):
        window = self.window
        panel = window.draft_panel
        panel.new()
        panel.add_blank("NAVIGATE")
        panel.table.item(0, 2).setText("station_a")
        log_view, log_card = window.logs, window.log_card
        window.append_log("layout-regression-entry")
        QTest.mouseClick(window.nav_buttons["日志分析"], Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(window.pages.currentIndex(), window.page_indexes["日志分析"])
        self.assertGreaterEqual(window.log_analysis_layout.indexOf(log_card), 0)
        self.assertIs(window.logs, log_view)
        self.assertIn("layout-regression-entry", log_view.toPlainText())
        for title in (
            "地图与定位",
            "机器人状态",
            "任务管理",
            "相机图像",
            "系统设置",
            "首页",
        ):
            QTest.mouseClick(window.nav_buttons[title], Qt.MouseButton.LeftButton)
            self.app.processEvents()
            self.assertTrue(window.nav_buttons[title].isChecked())
            self.assertEqual(window.pages.currentIndex(), window.page_indexes[title])
        self.assertIs(window.home_grid.itemAtPosition(2, 0).widget(), log_card)
        self.assertEqual(panel.steps()[0]["destination_station_id"], "station_a")
        self.assertTrue(panel.dirty)
        self.assertIs(
            window.catalog_panels["stations"].parentWidget(),
            window.settings_tabs.widget(1),
        )

    def test_header_and_progress_use_voice_state_without_claiming_motion(self):
        self.assertTrue(self.window.clock_timer.isActive())
        self.assertRegex(
            self.window.time_label.text(), r"\d{4}-\d{2}-\d{2}\n\d{2}:\d{2}:\d{2}"
        )
        state = dict(
            request_id="voice-one",
            source_node="/audio_processing_node",
            boot_id="boot",
            stamp_ns=1,
            event_seq=1,
            stage=2,
        )
        self.window.receive_record("state", state)
        self.assertIn("处理中", self.window.task_badge.text())
        self.assertEqual(self.window.stage_progress.value(), 0)
        self.window.receive_record(
            "state", dict(state, stamp_ns=2, event_seq=2, stage=5)
        )
        self.assertIn("待命", self.window.task_badge.text())
        self.assertEqual(self.window.stage_progress.value(), 0)
        self.assertIn("未确认", self.window.stage_progress.format())

    def test_debug_toggle_replays_history_and_errors_never_get_filtered(self):
        window = self.window
        self.assertFalse(window.show_debug_checkbox.isChecked())
        for text in ("[I] ready", "[D] queue=1", "[E] failed", "[W] degraded"):
            window.append_log(text)
        self.assertNotIn("queue=1", window.logs.toPlainText())
        self.assertIn("[E] failed", window.logs.toPlainText())
        self.assertIn("[W] degraded", window.logs.toPlainText())
        self.assertEqual(len(window.log_cache), 4)
        window.show_debug_checkbox.setChecked(True)
        self.assertIn("queue=1", window.logs.toPlainText())
        window.show_debug_checkbox.setChecked(False)
        self.assertNotIn("queue=1", window.logs.toPlainText())
        self.assertEqual(len(window.log_cache), 4)
        window.switch_page(window.page_indexes["日志分析"])
        window.show_debug_checkbox.setChecked(True)
        self.assertIn("queue=1", window.logs.toPlainText())
        QTest.mouseClick(window.clear_log_button, Qt.MouseButton.LeftButton)
        window.show_debug_checkbox.setChecked(False)
        window.show_debug_checkbox.setChecked(True)
        self.assertEqual(window.log_cache, [])
        self.assertEqual(window.logs.toPlainText(), "")

    def test_log_colors_escape_payload_markup_and_color_wrapped_lines(self):
        window = self.window
        window.show_debug_checkbox.setChecked(True)
        window.append_log('[E] <img src="invalid">' + " error" * 10)
        window.append_log("[D] trace detail")
        window.append_log("[W] degraded")
        self.app.processEvents()
        self.assertIn("&lt;img", window.logs.body.text())
        self.assertIn("#c85648", window.logs.body.text())
        self.assertNotIn('<img src="invalid">', window.logs.body.text())
        self.assertEqual(window.logs.COLORS["DEBUG"], "#8797ad")
        self.assertEqual(window.logs.COLORS["WARNING"], "#ac7c23")

    def test_simulation_renders_telemetry_without_creating_audio_history(self):
        config = SimpleNamespace(**vars(self.window.config), use_simulator=True)
        self.window.can_close = True
        self.window.close()
        self.window = DashboardWindow(config)
        window = self.window
        window.show()
        self.app.processEvents()
        window.receive_record(
            "robot_camera",
            {"width": 2, "height": 2, "step": 6, "data": bytes(range(12))},
        )
        window.receive_record(
            "robot_map",
            {
                "width": 4,
                "height": 4,
                "resolution": 0.05,
                "frame_id": "map",
                "origin_x": -5.0,
                "origin_y": -5.0,
                "data": [0] * 16,
            },
        )
        self.emit_odom(window, {"x": -2.0, "y": 0.0, "speed": 0.35, "angular": 0.0})
        window.receive_record("robot_battery", {"percentage": 0.82, "voltage": 24.2})
        self.assertFalse(window.camera_view.image.isNull())
        self.assertFalse(window.map_view.image.isNull())
        self.assertIn("82%", window.hardware_labels["电池电量"].text())
        self.assertEqual(window.history.count(), 0)
        # Telemetry arrives while the navigation pages are hidden, including a
        # map published only once. Entering each page must display its cache.
        self.assertIsNot(window.map_view, window.map_page_view)
        self.assertIsNot(window.camera_view, window.camera_page_view)
        for title, view in (
            ("地图与定位", window.map_page_view),
            ("相机图像", window.camera_page_view),
        ):
            window.switch_page(window.page_indexes[title])
            self.app.processEvents()
            self.assertTrue(view.isVisible())
            self.assertFalse(view.image.isNull())
            self.assertFalse(view.pixmap().isNull())
        window.switch_page(window.page_indexes["机器人状态"])
        self.app.processEvents()
        network_before = window.hardware_labels["网络连接"].text()
        window.receive_record(
            "robot_status",
            {
                "camera_ready": True,
                "chassis_ready": True,
                "network_connected": True,
                "network_latency_ms": 8.2,
                "ip": "127.0.0.1",
                "mode": "模拟巡航",
            },
        )
        self.assertIn("82%", window.hardware_page_labels["电池电量"].text())
        self.assertIn("-2.00", window.hardware_page_labels["机器人状态"].text())
        self.assertIn("rgb8", window.hardware_page_labels["相机状态"].text())
        self.assertEqual(window.hardware_page_labels["网络连接"].text(), network_before)
        self.assertNotIn("SIM 网络", network_before)
        self.assertEqual(window.robot_page_battery.value(), 82)
        window.receive_record(
            "robot_status",
            {
                "world_fresh": True,
                "chassis_ready": True,
                "camera_ready": True,
                "held_object_id": "box_1",
                "state": "RUNNING",
                "mode": "统一模拟执行 · 执行中",
                "stations": {"station_a": [-2.0, 0.0], "station_b": [2.0, 0.0]},
                "objects": [
                    {
                        "id": "box_1",
                        "carried": True,
                        "station_id": "",
                        "x": -2.0,
                        "y": 0.0,
                    }
                ],
            },
        )
        self.assertIn("未确认", window.material_state_label.text())
        window.receive_record("robot_heartbeat", dict(boot_id="typed-boot"))
        window.receive_record(
            "robot_material",
            dict(
                boot_id="typed-boot",
                event_seq=1,
                frame_id="map",
                known=True,
                held_object_id="box_1",
                objects=[
                    dict(
                        id="box_1",
                        station_id="",
                        carried=True,
                        position_known=True,
                        x=-2.0,
                        y=0.0,
                    )
                ],
            ),
        )
        self.assertEqual(window.material_state_label.text(), "持有物料：box_1")
        self.assertIn("随机器人携带", window.material_state_label.toolTip())
        window.receive_record(
            "robot_status",
            {
                "world_fresh": True,
                "chassis_ready": True,
                "camera_ready": True,
                "held_object_id": "",
                "state": "SUCCEEDED",
                "mode": "统一模拟执行 · 已完成",
                "objects": [
                    {
                        "id": "box_1",
                        "carried": False,
                        "station_id": "station_b",
                        "x": 2.0,
                        "y": 0.0,
                    }
                ],
            },
        )
        window.receive_record(
            "robot_material",
            dict(
                boot_id="typed-boot",
                event_seq=2,
                frame_id="map",
                known=True,
                held_object_id="",
                objects=[
                    dict(
                        id="box_1",
                        station_id="station_b",
                        carried=False,
                        position_known=True,
                        x=2.0,
                        y=0.0,
                    )
                ],
            ),
        )
        self.assertEqual(window.material_state_label.text(), "持有物料：无")
        self.assertIn("station_b", window.material_state_label.toolTip())
        window.receive_record("robot_battery", {"percentage": 0.63, "voltage": 24.0})
        self.assertEqual(
            window.hardware_labels["电池电量"].text(),
            window.hardware_page_labels["电池电量"].text(),
        )
        self.assertEqual(window.robot_page_battery.value(), 63)
        self.assertFalse(window.pages.findChildren(QScrollArea))
        window.switch_page(window.page_indexes["首页"])
        self.app.processEvents()
        self.assertTrue(window.map_view.isVisible())
        self.assertTrue(window.camera_view.isVisible())
        window.receive_record(
            "robot_task",
            {
                "task_id": "sim-one",
                "progress": 0.25,
                "active": True,
                "step": "2/4 抓取 box_1",
                "message": "模拟抓取",
            },
        )
        self.assertEqual(window.stage_progress.value(), 25)
        self.assertIn("sim-one", window.hardware_page_labels["当前任务"].text())
        sent = []
        window.draft_requested.connect(lambda operation, _: sent.append(operation))
        QTest.mouseClick(window.cancel_simulation_button, Qt.MouseButton.LeftButton)
        self.assertEqual(sent, ["cancel_execution"])
        window.receive_record(
            "robot_task",
            {
                "task_id": "sim-one",
                "progress": 0.25,
                "active": False,
                "failed": True,
                "message": "模拟执行已取消",
            },
        )
        self.assertEqual(window.stage_progress.value(), 25)
        self.assertFalse(window.cancel_simulation_button.isEnabled())

    def test_simulated_telemetry_expires_without_messages_and_recovers_on_new_samples(
        self,
    ):
        self._prepare_home_candidate()
        window, view = self.window, self.window.simulation_view
        window.receive_record("robot_heartbeat", dict(boot_id="freshness-boot"))
        samples = {
            "robot_camera": dict(width=2, height=2, step=6, data=bytes(range(12))),
            "robot_odom": dict(x=-2.0, y=0.0, speed=0.4, angular=0.0),
            "robot_battery": dict(percentage=0.82, voltage=24.2),
            "robot_status": dict(
                world_fresh=True, chassis_ready=True, held_object_id="box_1"
            ),
            "robot_material": dict(
                boot_id="freshness-boot",
                known=True,
                frame_id="map",
                held_object_id="box_1",
                objects=[],
            ),
        }
        with patch(
            "robot_visualization.ui.simulation.time.monotonic", return_value=10.0
        ):
            for kind, sample in samples.items():
                window.receive_record(
                    kind, dict(sample, received_at=10.0, stamp_ns=1, event_seq=1)
                )
            view.check_freshness()
            self.assertFalse(window.camera_view.stale)
            self.assertTrue(window.robot_page_battery.isEnabled())
            self.assertIn("box_1", window.material_state_label.text())
        with patch(
            "robot_visualization.ui.simulation.time.monotonic", return_value=12.1
        ):
            # Replayed source stamps must not keep old sensor data alive.
            for kind, sample in samples.items():
                window.receive_record(
                    kind, dict(sample, received_at=12.1, stamp_ns=1, event_seq=1)
                )
            view.check_freshness()
            self.assertTrue(window.camera_view.stale)
            self.assertTrue(window.camera_page_view.stale)
            self.assertIn("已过期", window.camera_page_info.text())
            self.assertIn("已过期", window.hardware_labels["机器人状态"].text())
            self.assertIn("未确认", window.material_state_label.text())
            self.assertFalse(window.robot_page_battery.isEnabled())
            self.assertEqual(window.robot_page_battery.value(), 82)
            self.assertIn("已过期", window.robot_page_battery.format())
        with patch(
            "robot_visualization.ui.simulation.time.monotonic", return_value=12.2
        ):
            for kind, sample in samples.items():
                window.receive_record(
                    kind, dict(sample, received_at=12.2, stamp_ns=2, event_seq=2)
                )
            view.check_freshness()
            self.assertFalse(window.camera_view.stale)
            self.assertTrue(window.robot_page_battery.isEnabled())
            self.assertNotIn("已过期", window.hardware_labels["机器人状态"].text())
            self.assertIn("box_1", window.material_state_label.text())

    def test_unknown_execution_preserves_progress_and_blocks_new_home_dispatch(self):
        parsed = self._prepare_home_candidate()
        window = self.window
        window.receive_record(
            "robot_task",
            dict(
                task_id="original-task",
                draft_id="original-draft",
                draft_revision=1,
                active=True,
                failed=False,
                progress=0.25,
                step="2/4 抓取 box_1",
                state=1,
            ),
        )
        window.receive_record(
            "robot_task",
            dict(
                task_id="original-task",
                active=False,
                execution_unknown=True,
                failed=False,
                state=None,
                cancel_available=True,
                message="执行反馈已失联，结果未知",
                retryable=False,
            ),
        )
        window.receive_record("parse", dict(parsed, request_id="new-candidate"))
        self.assertEqual(window.stage_progress.value(), 25)
        self.assertIn("结果未知", window.stage_badge.text())
        self.assertIn("结果未知", window.home_draft_status.text())
        self.assertFalse(window.home_add_candidate_button.isEnabled())
        self.assertFalse(window.home_confirm_draft_button.isEnabled())
        self.assertFalse(window.retry_simulation_button.isVisible())
        self.assertTrue(window.cancel_simulation_button.isEnabled())
        window.receive_record(
            "robot_task",
            dict(
                task_id="original-task",
                canceling=True,
                execution_unknown=True,
            ),
        )
        self.assertFalse(window.cancel_simulation_button.isEnabled())
        window.receive_record(
            "robot_task",
            dict(
                task_id="original-task",
                active=False,
                execution_unknown=False,
                canceling=False,
                failed=False,
                state=3,
                progress=1.0,
                message="完成",
            ),
        )
        self.assertEqual(window.stage_progress.value(), 100)
        self.assertTrue(window.home_add_candidate_button.isEnabled())

    def test_home_prepare_retry_only_uses_matching_confirmed_unedited_draft(self):
        self._prepare_home_candidate()
        window = self.window
        panel = window.draft_panel
        panel.add_candidate()
        saved = dict(
            draft_id="retry-draft",
            revision=1,
            event_seq=1,
            boot_id="boot-one",
            state=1,
            session_id=panel.session,
            input_context_id=panel.context,
            source_request_ids=list(panel.sources),
            candidate=panel.plan(),
            confirmation_token="token-one",
            summary="四步搬运",
        )
        panel.apply_record(saved)
        task = dict(
            task_id="prepare-one",
            draft_id="retry-draft",
            draft_revision=1,
            active=False,
            failed=True,
            state=4,
            retryable=True,
            execution_unknown=False,
            message="准备被拒绝，尚未发送执行 Goal",
        )
        window.receive_record("robot_task", task)
        self.assertTrue(window.retry_simulation_button.isEnabled())
        sent = []
        window.draft_requested.connect(
            lambda operation, body: sent.append((operation, body))
        )
        QTest.mouseClick(window.retry_simulation_button, Qt.MouseButton.LeftButton)
        self.assertEqual(sent[0][0], "prepare_execution")
        self.assertEqual(sent[0][1]["draft_id"], "retry-draft")
        self.assertEqual(sent[0][1]["revision"], 1)
        window.receive_record(
            "robot_task", dict(task, execution_unknown=True, retryable=False)
        )
        self.assertFalse(window.retry_simulation_button.isVisible())
        window.receive_record("robot_task", task)
        panel.changed()
        window.home_drafts.refresh()
        self.assertFalse(window.retry_simulation_button.isEnabled())

    def _prepare_home_candidate(self, use_simulator=True):
        self.window.can_close = True
        self.window.close()
        self.window = DashboardWindow(
            SimpleNamespace(
                model="qwen3.5:2b",
                stations=["station_a", "station_b"],
                objects=["box_1"],
                audio_root=Path("/tmp/audio"),
                use_simulator=use_simulator,
            )
        )
        self.window.show()
        self.app.processEvents()
        self.window.receive_availability(dict(self.value, drafts=True))
        parsed = dict(
            request_id="home-audio",
            session_id="voice-session",
            input_context_id="voice-context",
            success=True,
            outcome=0,
            candidate=dict(
                task_type=2,
                object_id="box_1",
                pickup_station_id="station_a",
                dropoff_station_id="station_b",
            ),
        )
        self.window.receive_record("parse", parsed)
        return parsed

    def test_network_card_uses_local_interface_data_and_clears_stale_ip(self):
        with patch(
            "robot_visualization.ui.network_status.read_active_interfaces",
            return_value=["eth0: 192.0.2.10"],
        ):
            self.window.network_status.refresh()
        text = self.window.hardware_labels["网络连接"].text()
        self.assertIn("192.0.2.10", text)
        self.assertIn("端到端延迟：未测量", text)
        self.assertNotIn("SIM", text)
        with patch(
            "robot_visualization.ui.network_status.read_active_interfaces",
            return_value=[],
        ):
            self.window.network_status.refresh()
        self.assertIn("未发现", self.window.hardware_labels["网络连接"].text())
        self.assertNotIn("192.0.2.10", self.window.hardware_labels["网络连接"].text())

    def test_home_candidate_save_confirm_execute_never_requires_page_switch(self):
        self._assert_home_workflow(use_simulator=True)

    def test_real_deployment_uses_the_same_home_telemetry_and_confirmation_flow(self):
        self._assert_home_workflow(use_simulator=False)

    def _assert_home_workflow(self, use_simulator):
        parsed = self._prepare_home_candidate(use_simulator)
        window = self.window
        sent = []
        window.draft_requested.connect(
            lambda operation, body: sent.append((operation, body))
        )
        self.assertEqual(window.pages.currentIndex(), window.page_indexes["首页"])
        self.assertTrue(window.home_add_candidate_button.isEnabled())
        self.assertEqual(window.stage_progress.value(), 0)
        QTest.mouseClick(window.home_add_candidate_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(window.home_add_candidate_button, Qt.MouseButton.LeftButton)
        self.assertEqual([operation for operation, _ in sent], ["save"])
        self.assertEqual(window.draft_panel.table.rowCount(), 4)
        self.assertFalse(window.home_confirm_draft_button.isEnabled())
        body = sent[0][1]
        saved = dict(
            draft_id="home-draft",
            revision=1,
            event_seq=1,
            boot_id="home-boot",
            state=0,
            session_id=body["session_id"],
            input_context_id=body["input_context_id"],
            source_request_ids=list(body["source_request_ids"]),
            candidate=body["candidate"],
            confirmation_token="home-token",
            summary="A 取货后到 B 放下",
        )
        window.receive_draft(dict(operation="save", success=True, draft=saved))
        self.assertEqual(window.command_pages.currentIndex(), 1)
        self.assertIn("导航", window.home_draft_preview.toPlainText())
        self.assertFalse(window.home_confirm_draft_button.isEnabled())
        self.assertEqual(
            [operation for operation, _ in sent], ["save", "prepare_execution"]
        )
        self.deliver_preflight(saved)
        self.assertTrue(window.home_confirm_draft_button.isEnabled())
        self.assertIn("预检通过", window.home_draft_status.text())
        QTest.mouseClick(window.home_confirm_draft_button, Qt.MouseButton.LeftButton)
        self.assertEqual(sent[-1][0], "confirm")
        self.assertEqual(sent[-1][1]["confirmation_token"], "home-token")
        window.receive_draft(
            dict(
                operation="confirm",
                success=True,
                draft=dict(saved, state=1, event_seq=2),
            )
        )
        self.assertEqual(
            [operation for operation, _ in sent],
            ["save", "prepare_execution", "confirm", "execute"],
        )
        task = dict(
            task_id="home-task",
            draft_id="home-draft",
            draft_revision=1,
            active=True,
            failed=False,
            state=1,
            prepared=False,
        )
        window.receive_record("robot_task", dict(task, progress=0.0, step=""))
        self.assertEqual(window.stage_progress.value(), 0)
        window.receive_record(
            "robot_task", dict(task, progress=0.25, step="2/4 抓取 box_1")
        )
        self.assertEqual(window.home_draft_status.text(), "执行中，步骤 2/4")
        self.assertEqual(window.stage_progress.value(), 25)
        window.receive_record(
            "robot_map",
            {
                "width": 200,
                "height": 200,
                "resolution": 0.05,
                "frame_id": "map",
                "origin_x": -5.0,
                "origin_y": -5.0,
                "data": [0] * 40000,
            },
        )
        self.emit_odom(window, {"x": -2.0, "y": 0.0, "speed": 0.35, "angular": 0.0})
        old_color = window.map_view.image.pixelColor(180, 300)
        self.emit_odom(window, {"x": 2.0, "y": 0.0, "speed": 0.35, "angular": 0.0})
        self.assertNotEqual(window.map_view.image.pixelColor(180, 300), old_color)
        window.receive_record(
            "robot_task",
            dict(task, active=False, progress=1.0, state=3, step="4/4 放下 box_1"),
        )
        self.assertEqual(window.home_draft_status.text(), "执行完成")
        self.assertEqual(window.stage_progress.value(), 100)
        self.assertFalse(window.home_confirm_draft_button.isEnabled())
        self.assertEqual(window.pages.currentIndex(), window.page_indexes["首页"])

        window.receive_record("parse", dict(parsed, request_id="home-audio-next"))
        QTest.mouseClick(window.home_add_candidate_button, Qt.MouseButton.LeftButton)
        next_body = sent[-1][1]
        next_draft = dict(
            saved,
            draft_id="home-draft-next",
            state=0,
            source_request_ids=list(next_body["source_request_ids"]),
        )
        window.receive_draft(dict(operation="save", success=True, draft=next_draft))
        self.assertIn("等待预检", window.home_draft_status.text())
        self.deliver_preflight(next_draft)
        QTest.mouseClick(window.home_confirm_draft_button, Qt.MouseButton.LeftButton)
        window.receive_draft(
            dict(
                operation="confirm",
                success=True,
                draft=dict(next_draft, state=1, event_seq=2),
            )
        )
        window.receive_record(
            "robot_task",
            {
                "task_id": "",
                "draft_id": "home-draft-next",
                "draft_revision": 1,
                "active": False,
                "failed": True,
                "state": 4,
                "message": "模拟服务尚未就绪",
            },
        )
        self.assertIn("执行失败", window.home_draft_status.text())
        self.assertEqual(window.stage_progress.value(), 0)

    def deliver_preflight(self, saved):
        self.window.receive_record(
            "robot_task",
            dict(
                task_id="prepared-" + saved["draft_id"],
                draft_id=saved["draft_id"],
                draft_revision=saved["revision"],
                catalog_revision=self.window.catalog_snapshot["revision"],
                draft_boot_id=saved["boot_id"],
                active=False,
                failed=False,
                execution_unknown=False,
                prepared=True,
                preparation_pending=False,
                retryable=False,
                state=0,
                canceling=False,
                prepared_until=time.monotonic() + 300,
                prepared_candidate=saved["candidate"],
                progress=0.0,
                message="预检通过，请核对草稿",
            ),
        )

    def test_stale_preflight_and_unsolicited_review_cannot_start_execution(self):
        self._prepare_home_candidate()
        window, panel = self.window, self.window.draft_panel
        panel.add_candidate()
        saved = dict(
            draft_id="reviewed",
            revision=1,
            event_seq=1,
            boot_id="boot-one",
            state=0,
            session_id=panel.session,
            input_context_id=panel.context,
            source_request_ids=list(panel.sources),
            candidate=panel.plan(),
            confirmation_token="token",
            summary="搬运",
        )
        window.receive_draft(dict(operation="save", success=True, draft=saved))
        self.deliver_preflight(saved)
        sent = []
        window.draft_requested.connect(lambda operation, body: sent.append(operation))
        window.receive_draft(
            dict(
                operation="confirm",
                success=True,
                draft=dict(saved, state=1, event_seq=2),
            )
        )
        self.assertNotIn("execute", sent)
        # Expired preflight disables both home and editor confirmation controls.
        window.simulation_view.task["prepared_until"] = 0
        window.home_drafts.refresh()
        self.assertFalse(window.home_confirm_draft_button.isEnabled())
        self.assertFalse(panel.buttons["confirm"].isEnabled())

    def test_home_save_failure_retains_steps_and_cannot_confirm(self):
        self._prepare_home_candidate()
        window = self.window
        sent = []
        window.draft_requested.connect(lambda operation, body: sent.append(operation))
        QTest.mouseClick(window.home_add_candidate_button, Qt.MouseButton.LeftButton)
        window.receive_draft(
            dict(operation="save", success=False, message="资料已更新，请重新核对")
        )
        self.assertEqual(window.draft_panel.table.rowCount(), 4)
        self.assertIn("草稿未保存", window.home_draft_status.text())
        self.assertIn("资料已更新", window.home_draft_status.text())
        self.assertFalse(window.home_confirm_draft_button.isEnabled())
        self.assertEqual(sent, ["save"])
        QTest.mouseClick(window.home_save_draft_button, Qt.MouseButton.LeftButton)
        self.assertEqual(sent, ["save", "save"])
        window.receive_draft(
            dict(
                operation="save", success=False, unknown=True, message="保存结果未确认"
            )
        )
        self.assertFalse(window.home_confirm_draft_button.isEnabled())
        self.assertFalse(window.home_save_draft_button.isEnabled())
        self.assertEqual(sent, ["save", "save"])

    def test_home_add_preserves_unsaved_manual_steps_and_rejects_incomplete_candidate(
        self,
    ):
        parsed = self._prepare_home_candidate()
        window = self.window
        panel = window.draft_panel
        panel.new()
        panel.add_blank("NAVIGATE")
        panel.table.item(0, 2).setText("station_b")
        window.home_drafts.refresh()
        self.assertFalse(window.home_add_candidate_button.isEnabled())
        QTest.mouseClick(window.home_add_candidate_button, Qt.MouseButton.LeftButton)
        self.assertEqual(panel.steps()[0]["destination_station_id"], "station_b")
        panel.new()
        window.receive_record(
            "parse",
            dict(
                parsed,
                request_id="needs-material",
                success=False,
                outcome=1,
                clarification_question="搬运哪个物料？",
            ),
        )
        self.assertFalse(window.home_add_candidate_button.isEnabled())
        self.assertFalse(window.home_confirm_draft_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
