"""语音工作台窗口：展示真实 ROS 状态与结果，异步发起录音操作。"""

import json
import time

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from robot_visualization.state import STAGES, RecordStore
from robot_visualization.ui.theme import STYLE
from robot_visualization.ui.widgets import Badge, Card, PipelineStrip, glyph, label
from robot_visualization.ui.task_drafts import TaskDraftPanel
from robot_visualization.ui.paging import ElidedLabel, PagedList, PagedText, PageTabs
from robot_visualization.task_model import normalize_candidate
from ai_edge.task_drafts.store import plan_summary
from ai_edge.task_drafts.actions import action_steps


class DashboardWindow(QMainWindow):
    """Qt 展示与交互层；模型加载、ROS 通信和进程管理由后台负责。"""

    start_requested = Signal()
    stop_requested = Signal()
    query_requested = Signal(str)
    shutdown_requested = Signal()
    draft_requested = Signal(str, dict)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.store = RecordStore(limit=100)
        self.selected_request = None
        self.availability = {}
        self.control_pending = False
        self.recording_since = None
        self.can_close = False
        self.closing = False
        self.setWindowTitle("AI 语音交互工作台")
        self.setMinimumSize(880, 620)
        self.resize(1240, 840)
        self.setStyleSheet(STYLE)
        self.build_ui()
        self.refresh_controls()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(200)
        self.shortcuts = []
        for key, callback in (
            ("Alt+R", self.start_recording),
            ("Alt+S", self.stop_recording),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)

    def build_ui(self):
        """组合页面结构；各区域的控件在独立方法中创建。"""
        canvas = QWidget()
        canvas.setObjectName("canvas")
        self.setCentralWidget(canvas)
        layout = QVBoxLayout(canvas)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())
        layout.addWidget(self._build_content())

    def _build_header(self):
        """创建页面标题与 AI 编排连接标记。"""
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(label("AI 语音交互工作台", "title"))
        heading.addWidget(label("真实录音 · 语音识别 · 任务理解", "subtitle"))
        header.addLayout(heading)
        header.addStretch()
        self.connection_badge = Badge()
        header.addWidget(self.connection_badge)
        return header

    def _build_content(self):
        """固定页面切换；列表与文本各自分页，不使用外层滚动区域。"""
        self.pages = PageTabs()
        for title, build in (
            ("语音输入", self._build_recording_card),
            ("识别结果", self._build_result_card),
            ("任务草稿", self._build_draft_card),
            ("历史记录", self._build_history_card),
            ("服务状态", self._build_service_card),
            ("运行日志", self._build_log_card),
        ):
            self.pages.addTab(build(), title)
        return self.pages

    def _build_recording_card(self):
        """创建录音提示、状态标签和开始／停止按钮。"""
        recording = Card(
            "语音输入",
            f"说完后停止并识别；单段最长 {getattr(self.config, 'max_recording_seconds', 120.0):g} 秒，可分段录音加入同一草稿。",
        )
        self.capture_badge = Badge("设备状态未确认")
        recording.heading.addWidget(self.capture_badge)
        icon = label()
        icon.setPixmap(glyph("microphone", 64))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        recording.body.addWidget(icon)
        self.recording_label = label("等待麦克风与模型连接", "hero", True)
        self.recording_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        recording.body.addWidget(self.recording_label)
        self.capture_hint = PagedText("录音状态以设备广播为准")
        recording.body.addWidget(self.capture_hint, 1)
        controls = QHBoxLayout()
        self.start_button = QPushButton("开始录音  Alt+R")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_recording)
        self.stop_button = QPushButton("停止并识别  Alt+S")
        self.stop_button.setObjectName("stop")
        self.stop_button.clicked.connect(self.stop_recording)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        recording.body.addLayout(controls)
        self.control_message = PagedText()
        recording.body.addWidget(self.control_message, 1)
        return recording

    def _build_service_card(self):
        """创建麦克风、ASR 等服务状态与当前配置展示。"""
        services = Card("服务状态", "状态来自服务发现与设备广播。")
        self.service_badges = {}
        for name, title in (
            ("microphone", "麦克风"),
            ("receiver", "音频接收"),
            ("asr", "语音识别"),
            ("parser", "语义解析"),
            ("processor", "AI 编排"),
            ("drafts", "任务草稿"),
        ):
            row = QHBoxLayout()
            row.addWidget(label(title))
            row.addStretch()
            badge = Badge()
            self.service_badges[name] = badge
            row.addWidget(badge)
            services.body.addLayout(row)
        stations = "、".join(self.config.stations) or "未配置"
        objects = "、".join(self.config.objects) or "未配置"
        services.body.addWidget(
            PagedText(
                f"语义模型：{self.config.model}\n工位：{stations}\n物料：{objects}"
            ),
            1,
        )
        return services

    def _build_result_card(self):
        """创建处理阶段、识别文字、任务候选及结果文件展示。"""
        results = Card("识别与任务候选", "候选结果仍需经过任务准备和用户确认。")
        self.stage_badge = Badge("等待音频")
        results.heading.addWidget(self.stage_badge)
        self.pipeline = PipelineStrip()
        results.body.addWidget(self.pipeline)
        self.request_label = ElidedLabel("尚无处理记录")
        results.body.addWidget(self.request_label)
        self.result_pages = PageTabs()
        self.asr_text = PagedText()
        self.candidate_text = PagedText()
        self.result_message = PagedText()
        self.result_pages.addTab(self.asr_text, "识别文字")
        self.result_pages.addTab(self.candidate_text, "动作预览")
        self.result_pages.addTab(self.result_message, "处理详情")
        results.body.addWidget(self.result_pages, 1)
        self.dialogue_hint = ElidedLabel()
        self.dialogue_hint.hide()
        results.body.addWidget(self.dialogue_hint)
        self.file_label = ElidedLabel(f"音频目录：{self.config.audio_root}")
        self.file_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        results.body.addWidget(self.file_label)
        return results

    def _build_history_card(self):
        """创建历史记录选择与按请求 ID 查询入口。"""
        history = Card("最近记录", "按请求 ID 查看已有结果；最多保留 100 条。")
        self.history = PagedList()
        self.history.currentItemChanged.connect(self.select_record)
        history.body.addWidget(self.history, 1)
        history.body.addWidget(self.history.pager)
        query_row = QHBoxLayout()
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("输入原始音频 request_id")
        self.query_input.returnPressed.connect(self.query_record)
        self.query_button = QPushButton("查询已有记录")
        self.query_button.clicked.connect(self.query_record)
        query_row.addWidget(self.query_input)
        query_row.addWidget(self.query_button)
        history.body.addLayout(query_row)
        return history

    def _build_log_card(self):
        """创建有行数上限的运行日志及清空显示按钮。"""
        logs = Card("运行日志")
        clear = QPushButton("清空显示")
        clear.setObjectName("small")
        self.logs = PagedText()
        self.logs.setMaximumBlockCount(1000)
        clear.clicked.connect(self.logs.clear)
        logs.heading.addWidget(clear)
        logs.body.addWidget(self.logs, 1)
        return logs

    def _build_draft_card(self):
        card = Card("任务草稿与确认", "按实际顺序增删步骤，保存后逐步核对并确认。")
        self.draft_panel = TaskDraftPanel(self.config)
        self.draft_panel.command_requested.connect(self.draft_requested.emit)
        self.draft_panel.log_message.connect(self.append_log)
        card.body.addWidget(self.draft_panel, 1)
        return card

    @Slot(dict)
    def receive_draft(self, value):
        self.draft_panel.receive(value)
        if value.get("message"):
            self.append_log(value["message"])

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
        self.append_log(value.get("message", ""))
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
        self.request_label.setText(f"请求：{self.selected_request or '—'}")
        self.asr_text.setPlainText(asr.get("text", ""))
        self._show_candidate(parsed)
        self.draft_panel.set_candidate(parsed)
        self._show_result_details(state, parsed)

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
                "候选信息已完整，可到任务草稿页加入候选并逐步核对；尚未执行。"
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
            self.logs.appendPlainText(str(text))

    def closeEvent(self, event):
        if self.can_close:
            self.timer.stop()
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
            self.receive_availability({})
            self.control_message.setText("后台线程已结束，请查看日志后重新启动工作台")
