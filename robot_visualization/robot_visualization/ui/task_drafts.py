"""可变行数的任务草稿编辑与人工确认界面。"""

from copy import deepcopy
from uuid import uuid4

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai_edge.semantic.semantic_contract import (
    ID_FIELDS,
    empty_candidate,
)
from ai_edge.task_drafts.store import plan_summary
from ai_edge.task_drafts.actions import action_steps, validate_action_plan
from robot_visualization.task_model import normalize_candidate


class TaskDraftPanel(QWidget):
    command_requested = Signal(str, dict)
    STATUS = {
        0: "待确认",
        1: "已确认草稿，尚未执行",
        2: "已取消",
        3: "已失效，需重新保存",
    }

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.saved = None
        self.records = {}
        self.candidate = None
        self.loading = False
        self.dirty = False
        self.pending = False
        self.online = False
        self.unknown = False
        self.sources = []
        self.session = "ui-session-" + uuid4().hex
        self.context = "ui-context-" + uuid4().hex
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.history = QComboBox()
        self.history.setPlaceholderText("已保存的草稿")
        self.history.activated.connect(self.select_saved)
        layout.addWidget(self.history)
        self.info = QLabel("新建草稿后可逐条添加任务；每个步骤都需要核对。")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["动作", "物料", "目标／操作工位"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.setMinimumHeight(240)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.cellChanged.connect(self.changed)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        self.buttons = {}
        actions = [
            ("new", "新建草稿", self.new),
            ("add", "加入当前候选", self.add_candidate),
            ("navigate", "+ 导航", lambda: self.add_blank("NAVIGATE")),
            ("pick", "+ 抓取", lambda: self.add_blank("PICK")),
            ("place", "+ 放下", lambda: self.add_blank("PLACE")),
            ("remove", "删除步骤", self.remove_step),
            ("up", "上移", lambda: self.move_step(-1)),
            ("down", "下移", lambda: self.move_step(1)),
        ]
        for key, title, callback in actions:
            button = QPushButton(title)
            button.clicked.connect(callback)
            self.buttons[key] = button
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        for key, title, callback in [
            ("save", "保存草稿", self.save),
            ("confirm", "确认草稿", self.confirm),
            ("cancel", "取消草稿", self.cancel),
            ("refresh", "刷新已保存版本", self.query),
        ]:
            button = QPushButton(title)
            button.clicked.connect(callback)
            self.buttons[key] = button
            row.addWidget(button)
        layout.addLayout(row)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMinimumHeight(120)
        self.summary.setMaximumHeight(240)
        self.summary.setPlainText("确认只记录人工审阅结果；导航和抓取执行尚未接入。")
        layout.addWidget(self.summary)
        self.refresh_controls()

    def set_online(self, online):
        self.online = bool(online)
        self.refresh_controls()

    def set_candidate(self, parsed):
        self.candidate = (
            deepcopy(parsed)
            if parsed.get("success") and parsed.get("outcome") == 0
            else None
        )
        self.refresh_controls()

    def refresh_controls(self):
        free = not self.pending
        self.table.setEnabled(free)
        self.history.setEnabled(free)
        for key in ("new", "navigate", "pick", "place", "remove", "up", "down"):
            self.buttons[key].setEnabled(free)
        self.buttons["add"].setEnabled(free and self.candidate is not None)
        self.buttons["save"].setEnabled(
            free and self.online and not self.unknown and self.table.rowCount() > 0
        )
        self.buttons["refresh"].setEnabled(free and self.online)
        reviewable = (
            free
            and self.online
            and not self.dirty
            and not self.unknown
            and self.saved is not None
        )
        self.buttons["confirm"].setEnabled(
            bool(reviewable and self.saved["state"] == 0)
        )
        self.buttons["cancel"].setEnabled(
            bool(reviewable and self.saved["state"] in (0, 1))
        )

    def changed(self, *args):
        if self.loading:
            return
        self.dirty = True
        self.info.setText(
            f"共 {self.table.rowCount()} 步；有未保存修改，请保存后重新核对。"
        )
        try:
            self.summary.setPlainText(plan_summary(self.plan()))
        except ValueError:
            self.summary.setPlainText("草稿还没有步骤。")
        self.refresh_controls()

    def insert_step(self, step):
        row = self.table.rowCount()
        self.table.insertRow(row)
        kind = QComboBox()
        for title, value in (
            ("导航到", "NAVIGATE"),
            ("抓取", "PICK"),
            ("放下", "PLACE"),
        ):
            kind.addItem(title, value)
        kind.setCurrentIndex(kind.findData(step["task_type"]))
        kind.currentIndexChanged.connect(self.changed)
        self.table.setCellWidget(row, 0, kind)
        self.table.setItem(row, 1, QTableWidgetItem(step["object_id"]))
        field = {
            "NAVIGATE": "destination_station_id",
            "PICK": "pickup_station_id",
            "PLACE": "dropoff_station_id",
        }[step["task_type"]]
        self.table.setItem(row, 2, QTableWidgetItem(step[field]))

    def steps(self):
        values = []
        for row in range(self.table.rowCount()):
            item = {field: "" for field in ID_FIELDS}
            kind = self.table.cellWidget(row, 0).currentData()
            item.update(task_type=kind, return_required=False)
            obj, station = self.table.item(row, 1), self.table.item(row, 2)
            item["object_id"] = obj.text().strip() if obj else ""
            field = {
                "NAVIGATE": "destination_station_id",
                "PICK": "pickup_station_id",
                "PLACE": "dropoff_station_id",
            }[kind]
            item[field] = station.text().strip() if station else ""
            values.append(item)
        return values

    def plan(self):
        values = self.steps()
        if not values:
            raise ValueError("请添加动作")
        return empty_candidate("SEQUENCE", values)

    def load_steps(self, values):
        self.loading = True
        try:
            self.table.setRowCount(0)
            for step in values:
                self.insert_step(step)
        finally:
            self.loading = False

    def new(self):
        self.saved = None
        self.sources = []
        self.session, self.context = (
            "ui-session-" + uuid4().hex,
            "ui-context-" + uuid4().hex,
        )
        self.unknown = False
        self.load_steps([])
        self.history.setCurrentIndex(-1)
        self.changed()

    def add_blank(self, kind):
        if self.table.rowCount() >= 256:
            self.info.setText("已达到256步保护上限，请分成多份草稿。")
            return
        step = empty_candidate(kind)
        step.pop("steps")
        values = self.steps()
        if kind in ("PICK", "PLACE"):
            station = next(
                (
                    item["destination_station_id"]
                    for item in reversed(values)
                    if item["task_type"] == "NAVIGATE"
                ),
                "",
            )
            step["pickup_station_id" if kind == "PICK" else "dropoff_station_id"] = (
                station
            )
            if kind == "PLACE":
                held = ""
                for item in values:
                    if item["task_type"] == "PICK":
                        held = item["object_id"]
                    elif item["task_type"] == "PLACE":
                        held = ""
                step["object_id"] = held
        self.load_steps([*values, step])
        self.changed()

    def add_candidate(self):
        if self.candidate is None:
            return
        value = self.candidate
        if value["request_id"] in self.sources:
            self.info.setText("这条录音候选已加入；可继续录音添加后续步骤。")
            return
        identity = value["session_id"], value["input_context_id"]
        if self.table.rowCount() and identity != (self.session, self.context):
            self.info.setText("候选属于另一会话，请先新建草稿。")
            return
        try:
            values = action_steps(normalize_candidate(value["candidate"]))
        except ValueError as exc:
            self.info.setText(str(exc))
            return
        if self.table.rowCount() + len(values) > 256:
            self.info.setText("添加后超过256步上限，未截断或部分加入。")
            return
        self.session, self.context = identity
        self.sources.append(value["request_id"])
        self.load_steps([*self.steps(), *values])
        self.changed()

    def remove_step(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self.changed()

    def move_step(self, direction):
        row = self.table.currentRow()
        other = row + direction
        if row < 0 or not 0 <= other < self.table.rowCount():
            return
        values = self.steps()
        values[row], values[other] = values[other], values[row]
        self.load_steps(values)
        self.table.selectRow(other)
        self.changed()

    def send(self, operation, value):
        self.pending = True
        self.info.setText("正在处理草稿请求……")
        self.refresh_controls()
        self.command_requested.emit(operation, value)

    def save(self):
        try:
            prepared = validate_action_plan(
                self.plan(), self.config.stations, self.config.objects
            )
            self.send(
                "save",
                dict(
                    draft_id=self.saved["draft_id"] if self.saved else "",
                    expected_revision=self.saved["revision"] if self.saved else 0,
                    session_id=self.session,
                    input_context_id=self.context,
                    source_request_ids=self.sources,
                    candidate=prepared,
                ),
            )
        except ValueError as exc:
            self.info.setText(str(exc))

    def confirm(self):
        if self.buttons["confirm"].isEnabled():
            self.send("confirm", deepcopy(self.saved))

    def cancel(self):
        if self.buttons["cancel"].isEnabled():
            self.send("cancel", deepcopy(self.saved))

    def query(self):
        self.send("query", {})

    def select_saved(self, index):
        key = self.history.itemData(index)
        if key in self.records:
            self.apply_record(self.records[key])

    def apply_record(self, value):
        self.saved = deepcopy(value)
        self.history.setCurrentIndex(self.history.findData(value["draft_id"]))
        self.sources = list(value["source_request_ids"])
        self.session, self.context = value["session_id"], value["input_context_id"]
        candidate = normalize_candidate(value["candidate"])
        try:
            expanded = action_steps(candidate)
        except ValueError as exc:
            self.saved = None
            self.load_steps([])
            self.dirty, self.unknown = True, False
            self.info.setText(f"{exc}；请新建并拆分草稿，原记录仍保留。")
            self.summary.setPlainText(value["summary"])
            self.refresh_controls()
            return
        self.load_steps(expanded)
        self.dirty = candidate != empty_candidate("SEQUENCE", expanded)
        self.unknown = False
        self.info.setText(
            f"{value['draft_id']} · 版本 {value['revision']} · "
            + (
                "旧草稿已展开，请保存新版本再确认"
                if self.dirty
                else self.STATUS.get(value["state"], "未知")
            )
        )
        self.summary.setPlainText(
            plan_summary(empty_candidate("SEQUENCE", expanded))
            + "\n确认只保存逐步动作的审阅结果，机器人尚未执行。"
        )
        self.refresh_controls()

    @Slot(dict)
    def receive(self, value):
        operation = value.get("operation")
        if operation != "event":
            self.pending = False
        if not value.get("success"):
            self.unknown = self.unknown or value.get("unknown", False)
            self.info.setText(value.get("message", "草稿请求失败"))
            self.refresh_controls()
            return
        incoming = value.get("drafts", [value["draft"]] if "draft" in value else [])
        accepted = []
        for draft in incoming:
            previous = self.records.get(draft["draft_id"])
            if previous and previous["event_seq"] > draft["event_seq"]:
                continue
            self.records[draft["draft_id"]] = draft
            accepted.append(draft)
        self.history.blockSignals(True)
        self.history.clear()
        for key, draft in self.records.items():
            self.history.addItem(
                f"{key} · v{draft['revision']} · {self.STATUS.get(draft['state'])}", key
            )
        self.history.blockSignals(False)
        if operation == "event":
            if (
                accepted
                and self.saved
                and accepted[0]["draft_id"] == self.saved["draft_id"]
                and not self.dirty
            ):
                self.apply_record(accepted[0])
        elif accepted:
            match = next(
                (
                    d
                    for d in accepted
                    if self.saved and d["draft_id"] == self.saved["draft_id"]
                ),
                accepted[0],
            )
            if operation != "query" or self.saved or not self.dirty:
                self.apply_record(match)
            elif self.unknown:
                matches = [
                    d
                    for d in accepted
                    if (d["session_id"], d["input_context_id"], d["source_request_ids"])
                    == (self.session, self.context, self.sources)
                ]
                if len(matches) == 1:
                    self.apply_record(matches[0])
                else:
                    self.info.setText("结果仍未确认，请从列表选择并核对已保存的草稿。")
        elif operation != "event":
            self.info.setText(
                "尚无已保存草稿。"
                if operation == "query"
                else "草稿版本已变化，请刷新后核对。"
            )
        self.history.setCurrentIndex(
            self.history.findData(self.saved["draft_id"]) if self.saved else -1
        )
        self.refresh_controls()
