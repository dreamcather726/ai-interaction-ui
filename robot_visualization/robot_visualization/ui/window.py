"""语音工作台窗口：展示真实 ROS 状态与结果，异步发起录音操作。"""

import json
import time
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QListWidgetItem,
    QMainWindow,
)

from robot_visualization.state import STAGES, RecordStore
from robot_visualization.ui.theme import STYLE
from robot_visualization.ui.monitor_layout import build_monitor_layout, move_log_card
from robot_visualization.ui.draft_workflow import HomeDraftWorkflow
from robot_visualization.ui.network_status import LocalNetworkStatus
from robot_visualization.runtime.console import format_log, log_level
from robot_visualization.task_model import normalize_candidate
from ai_edge.task_drafts.store import plan_summary
from ai_edge.task_drafts.actions import action_steps
from ai_edge.common.catalog import catalog_ids, seed_catalog


class DashboardWindow(QMainWindow):
    """Qt 展示与交互层；模型加载、ROS 通信和进程管理由后台负责。"""

    LAYOUT_DESCRIPTION = (
        "三栏监控工作台：顶部栏、左侧导航、首页双列内容、右侧状态栏；无主窗口滚动区"
    )

    start_requested = Signal()
    stop_requested = Signal()
    query_requested = Signal(str)
    shutdown_requested = Signal()
    draft_requested = Signal(str, dict)
    catalog_requested = Signal(str, dict)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.catalog_snapshot = getattr(
            config, "catalog_snapshot", None
        ) or seed_catalog(config.stations, config.objects)
        self.catalog_panels = {}
        self.log_cache = []
        self.store = RecordStore(limit=100)
        self.selected_request = None
        self.availability = {}
        self.control_pending = False
        self.recording_since = None
        self.can_close = False
        self.closing = False
        self.setWindowTitle("机器人可视化与监控平台 · 三栏监控版")
        # 普通窗口大小为1440×900；应用入口默认全屏，F11可恢复普通窗口。
        available = self.screen().availableGeometry()
        width = max(1, available.width() - 32)
        height = max(1, available.height() - 48)
        self.setMinimumSize(min(1200, width), min(720, height))
        self.resize(min(1440, width), min(900, height))
        self.setStyleSheet(STYLE)
        self.build_ui()
        from robot_visualization.ui.simulation import RobotView

        self.robot_view = RobotView(self)
        self.simulation_view = self.robot_view  # Compatibility alias.
        self.home_drafts = HomeDraftWorkflow(self)
        self.network_status = LocalNetworkStatus(self)
        self.refresh_controls()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(200)
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)
        self.update_clock()
        self.shortcuts = []
        for key, callback in (
            ("Alt+R", self.start_recording),
            ("Alt+S", self.stop_recording),
            ("F11", self.toggle_fullscreen),
            ("Esc", self.close),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)

    def build_ui(self):
        build_monitor_layout(self)

    @Slot()
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    @Slot(int)
    def switch_page(self, index):
        self.pages.setCurrentIndex(index)

    @Slot(int)
    def page_changed(self, index):
        button = self.navigation.button(index)
        if button is not None:
            button.setChecked(True)
        move_log_card(self, index)

    @Slot()
    def update_clock(self):
        current = datetime.now(timezone(timedelta(hours=8)))
        self.time_label.setText(current.strftime("%Y-%m-%d\n%H:%M:%S"))
        self.time_label.setToolTip("北京时间 · Asia/Shanghai")

    def refresh_task_badge(self):
        if self.robot_view is not None and self.robot_view.task:
            self.robot_view.render_task()
            return
        active = any(
            record.get("state", {}).get("stage") in (0, 1, 2, 3, 4)
            for record in self.store.records.values()
        )
        self.task_badge.update_status(
            "● 任务状态 · 处理中" if active else "● 任务状态 · 待命",
            "warn" if active else "good",
        )

    @Slot(dict)
    def receive_catalog(self, value):
        if value.get("snapshot"):
            self.catalog_snapshot = value["snapshot"]
            self.draft_panel.set_catalog(self.catalog_snapshot)
            stations, objects = catalog_ids(self.catalog_snapshot)
            self.catalog_details.setPlainText(
                f"语义模型：{self.config.model}\n工位：{'、'.join(stations) or '未配置'}"
                f"\n物料：{'、'.join(objects) or '未配置'}"
                f"\n资料版本：{self.catalog_snapshot['revision']}"
            )
        for panel in self.catalog_panels.values():
            panel.receive(value)
        if value.get("message"):
            self.append_log(
                format_log(
                    value["message"], "INFO" if value.get("success") else "ERROR"
                )
            )

    @Slot(dict)
    def receive_draft(self, value):
        self.draft_panel.receive(value)
        self.home_drafts.receive(value)
        if value.get("message"):
            self.append_log(
                format_log(
                    value["message"], "INFO" if value.get("success") else "ERROR"
                )
            )

    def refresh_controls(self):
        """服务连接、ASR 就绪、设备待机且无挂起操作时，才允许开始录音。"""
        value = self.availability
        available = not self.control_pending and not self.closing
        recording = value.get("recording")
        can_start = (
            available
            and value.get("start")
            and value.get("asr_ready")
            and recording is False
        )
        # 状态缺失时仍允许请求停止，不能将失联解释为录音已停止。
        can_stop = available and value.get("stop") and recording is not False
        self.start_button.setEnabled(bool(can_start))
        self.stop_button.setEnabled(bool(can_stop))
        self.query_button.setEnabled(bool(value.get("query") and not self.closing))
        self.draft_panel.setEnabled(not self.closing)
        self.draft_panel.set_online(value.get("drafts", False))
        self.home_drafts.refresh()
        for panel in self.catalog_panels.values():
            panel.setEnabled(not self.closing)

    def start_recording(self):
        if not self.start_button.isEnabled():
            return
        self.control_pending = True
        self.control_message.setText("正在请求开始录音……")
        self.refresh_controls()
        self.start_requested.emit()

    def stop_recording(self):
        if not self.stop_button.isEnabled():
            return
        self.control_pending = True
        self.control_message.setText("正在请求停止录音并发送……")
        self.refresh_controls()
        self.stop_requested.emit()

    @Slot(dict)
    def control_result(self, value):
        self.control_pending = False
        self.control_message.setText(value.get("message", ""))
        if value.get("message"):
            self.append_log(
                format_log(
                    value["message"], "INFO" if value.get("success") else "ERROR"
                )
            )
        # 成功回执不替代设备的实际录音状态；等待下一次状态广播。
        self.refresh_controls()

    @Slot(dict)
    def receive_availability(self, value):
        """接收 ROS 线程的状态快照；recording=None 表示设备状态未确认。"""
        previous = self.availability.get("recording")
        self.availability = value
        recording = value.get("recording")
        if recording is True and previous is not True:
            self.recording_since = time.monotonic()
        elif recording is not True:
            self.recording_since = None
        self.connection_badge.update_status(
            "AI 编排已连接" if value.get("connected") else "等待 AI 编排连接",
            "good" if value.get("connected") else "idle",
        )
        for name, badge in self.service_badges.items():
            text, tone = value.get("services", {}).get(name, ("等待连接", "idle"))
            badge.update_status(text, tone)
        self.capture_hint.setText(value.get("hint", "录音状态以设备广播为准"))
        self.refresh_controls()
        self.tick()

    def tick(self):
        if self.robot_view is not None:
            self.robot_view.check_freshness()
        recording = self.availability.get("recording")
        if recording is True:
            elapsed = max(0, time.monotonic() - self.recording_since)
            self.recording_label.setText(f"录音进程运行中 · 已观察 {elapsed:.1f} 秒")
            self.capture_badge.update_status("录音中", "active")
        elif recording is False:
            self.recording_label.setText("设备待机，可以开始新的录音")
            self.capture_badge.update_status("待机", "good")
        else:
            self.recording_label.setText("设备录音状态未确认")
            self.capture_badge.update_status("未确认", "warn")
        self.home_drafts.refresh()

    def query_record(self):
        request_id = self.query_input.text().strip()
        if request_id and self.query_button.isEnabled():
            if request_id in self.store.records:
                self.selected_request = request_id
                self.refresh_history()
                self.show_record()
            self.query_requested.emit(request_id)

    @Slot(str, dict)
    def receive_record(self, kind, value):
        if kind.startswith(("robot_", "sim_")):
            self.robot_view.receive(kind, value)
            if kind in ("robot_task", "sim_task"):
                self.home_drafts.refresh()
            return
        request_id = value.get("request_id")
        is_new = request_id not in self.store.records
        if not self.store.apply(kind, value):
            return
        if self.selected_request is None or is_new:
            self.selected_request = request_id
        if self.selected_request not in self.store.records:
            self.selected_request = next(reversed(self.store.records), None)
        self.refresh_history()
        self.show_record()
        self.refresh_task_badge()

    def refresh_history(self):
        self.history.blockSignals(True)
        try:
            self.history.clear()
            for request_id, record in reversed(self.store.records.items()):
                stage = record.get("state", {}).get("stage")
                title = STAGES.get(stage, ("等待处理状态", "idle"))[0]
                item = QListWidgetItem(f"{title}\n{request_id}")
                item.setData(Qt.ItemDataRole.UserRole, request_id)
                self.history.addItem(item)
                if request_id == self.selected_request:
                    self.history.setCurrentItem(item)
            self.history.refresh_pages()
        finally:
            self.history.blockSignals(False)

    def select_record(self, current, previous=None):
        if current is not None:
            self.selected_request = current.data(Qt.ItemDataRole.UserRole)
            self.show_record()

    def show_record(self):
        """按选中的请求 ID 渲染阶段、文字与候选，避免跨录音串用结果。"""
        record = self.store.records.get(self.selected_request, {})
        state = record.get("state", {})
        asr = record.get("asr", {})
        parsed = record.get("parse", {})
        stage = state.get("stage")
        title, tone = STAGES.get(stage, ("等待处理状态", "idle"))
        self.stage_badge.update_status(title, tone)
        self.pipeline.update_stage(
            stage,
            state.get("operation", ""),
            stage in (5, 6, 10),
            state.get("error_code", ""),
        )
        # Voice stages update PipelineStrip only. Robot progress is authoritative.
        self.request_label.setText(f"请求：{self.selected_request or '—'}")
        self.asr_text.setPlainText(asr.get("text", ""))
        self._show_candidate(parsed)
        self.draft_panel.set_candidate(parsed)
        self._show_result_details(state, parsed)
        if self.robot_view is not None and self.robot_view.task:
            self.robot_view.render_task()
        self.home_drafts.refresh()

    def _show_candidate(self, parsed):
        """展示单任务或有序步骤；草稿编辑独立于识别历史。"""
        is_ready = parsed.get("success") and parsed.get("outcome") == 0
        needs_clarification = parsed.get("outcome") == 1
        if is_ready or needs_clarification:
            candidate = normalize_candidate(parsed.get("candidate", {}))
            kinds = {
                "UNKNOWN": "未知",
                "NAVIGATE": "导航",
                "TRANSPORT": "搬运",
                "SEQUENCE": "有序任务列表",
                "PICK": "抓取",
                "PLACE": "放下",
            }
            try:
                preview = action_steps(candidate)
            except ValueError as exc:
                self.candidate_text.setPlainText(f"动作展开失败：{exc}")
                return
            self.candidate_text.setPlainText(
                f"任务类型：{kinds.get(candidate['task_type'], '未知')}\n"
                f"动作预览，共 {len(preview)} 步\n"
                + plan_summary({"task_type": "SEQUENCE", "steps": preview})
            )
        else:
            self.candidate_text.clear()

    def _show_result_details(self, state, parsed):
        """合并阶段说明、缺失字段、错误码及当前记录的音频路径。"""
        question = parsed.get("clarification_question", "")
        if parsed.get("outcome") == 1 and question:
            self.dialogue_hint.setText(
                f"{question}\n请再次录音回答；更换任务请先说“重新开始”。"
            )
        elif parsed.get("outcome") == 0 and parsed.get("success"):
            self.dialogue_hint.setText(
                "候选信息已完整，可加入草稿；在“任务草稿”页签核对完整动作后确认。"
            )
        else:
            self.dialogue_hint.clear()
        self.dialogue_hint.setVisible(bool(self.dialogue_hint.text()))
        messages = [state.get("message", ""), parsed.get("message", "")]
        if parsed.get("missing_fields"):
            messages.append("待补充：" + "、".join(parsed["missing_fields"]))
        if parsed.get("clarification_question"):
            messages.append(parsed["clarification_question"])
        error = parsed.get("error_code") or state.get("error_code")
        if error:
            messages.append(f"错误码：{error}")
        path = state.get("audio_path")
        messages.append(
            f"音频文件：{path}" if path else f"音频目录：{self.config.audio_root}"
        )
        self.result_message.setText("\n".join(dict.fromkeys(m for m in messages if m)))
        self.file_label.setText(
            f"音频文件：{path}" if path else f"音频目录：{self.config.audio_root}"
        )
        self.file_label.setToolTip(json.dumps(state, ensure_ascii=False, indent=2))

    @Slot(str)
    def append_log(self, text):
        if text:
            normalized = format_log(str(text), log_level(text))
            lines = normalized.split("\n")
            self.log_cache.extend(lines)
            visible = [
                line
                for line in lines
                if self.show_debug_checkbox.isChecked() or not line.startswith("[D]")
            ]
            if visible:
                self.logs.appendPlainText("\n".join(visible))

    @Slot(bool)
    def filter_logs(self, checked):
        visible = [
            line for line in self.log_cache if checked or not line.startswith("[D]")
        ]
        # 保留原面板1000行显示上限；完整会话缓存不按级别丢弃历史。
        self.logs.setPlainText("\n".join(visible[-1000:]))

    @Slot()
    def clear_logs(self):
        self.log_cache.clear()
        self.logs.clear()

    def closeEvent(self, event):
        if self.can_close:
            self.timer.stop()
            self.clock_timer.stop()
            self.network_status.stop()
            event.accept()
            return
        event.ignore()
        if not self.closing:
            self.closing = True
            self.control_message.setText("正在停止本窗口创建的后台进程，请稍候……")
            self.refresh_controls()
            self.shutdown_requested.emit()

    def finish_close(self):
        self.can_close = True
        QTimer.singleShot(0, self.close)

    @Slot()
    def on_worker_finished(self):
        if self.closing:
            self.finish_close()
        else:
            self.system_badge.update_status("● 通信线程已结束", "warn")
            self.receive_availability({})
            self.control_message.setText("后台线程已结束，请查看日志后重新启动工作台")
