"""分页维护基础资料，异步保存，不在界面内创建表格行内编辑器。"""

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from robot_visualization.ui.paging import ElidedLabel, PagedCombo, PagedTable


class EntityPicker(PagedCombo):
    textEdited = Signal(str)

    def __init__(self):
        super().__init__()
        self.entities = []
        self.setFocusProxy(self.box)
        self.activated.connect(lambda index: self.textEdited.emit(self.text()))

    def set_entities(self, entities):
        value = self.text()
        self.entities = [row for row in entities if row["enabled"]]
        super().clear()
        self.addItem("— 留空／待补充 —", "")
        for row in self.entities:
            self.addItem(f"{row['name']} · {row['id']}", row["id"])
        self.setText(value)

    def text(self):
        return self.itemData(self._current) or ""

    def setText(self, value):
        index = self.findData(value)
        if index < 0:
            self.addItem(f"未配置或已停用 · {value}", value)
            index = self.findData(value)
        self.setCurrentIndex(index)

    def clear(self):
        self.setText("")


class CatalogPanel(QWidget):
    command_requested = Signal(str, dict)

    def __init__(self, kind, snapshot):
        super().__init__()
        self.kind, self.snapshot = kind, snapshot
        self.pending = False
        self.selected_id = ""
        self.dirty = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.info = ElidedLabel(
            "新增资料后填写ID、名称和语音别名；保存后供新任务使用。"
        )
        layout.addWidget(self.info)
        self.table = PagedTable(4)
        self.table.keep_empty_selection = True
        self.table.setHorizontalHeaderLabels(["ID", "名称", "语音别名", "状态"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.setEditTriggers(PagedTable.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(PagedTable.SelectionBehavior.SelectRows)
        self.table.currentCellChanged.connect(self.select_row)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.table.pager)
        form = QFormLayout()
        self.identity, self.name, self.aliases = QLineEdit(), QLineEdit(), QLineEdit()
        self.aliases.setPlaceholderText("逗号分隔，例如：A点，取货区；最多8个")
        self.enabled = QCheckBox("启用，允许新任务选择")
        self.enabled.setChecked(True)
        for title, field in (
            ("ID", self.identity),
            ("名称", self.name),
            ("语音别名", self.aliases),
            ("状态", self.enabled),
        ):
            form.addRow(title, field)
        for field in (self.identity, self.name, self.aliases):
            field.textEdited.connect(self.mark_dirty)
        self.enabled.clicked.connect(self.mark_dirty)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.buttons = {}
        for key, title, callback in (
            ("new", "新增", self.new),
            ("save", "保存资料", self.save),
            ("delete", "删除选中资料", self.delete),
            ("refresh", "刷新资料", self.refresh),
        ):
            button = QPushButton(title)
            button.clicked.connect(callback)
            self.buttons[key] = button
            row.addWidget(button)
        layout.addLayout(row)
        self.populate()

    def mark_dirty(self, *args):
        self.dirty = True
        self.table.setEnabled(False)
        self.table.pager.setEnabled(False)
        self.info.setText("有未保存修改；保存后再切换记录，或点击新增／刷新放弃输入。")

    def populate(self):
        selected = self.selected_id
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for index, entity in enumerate(self.snapshot[self.kind]):
            self.table.insertRow(index)
            values = [
                entity["id"],
                entity["name"],
                "、".join(entity["aliases"]),
                "启用" if entity["enabled"] else "停用",
            ]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(value))
            if entity["id"] == selected:
                self.table.setCurrentCell(index, 0)
        self.table.blockSignals(False)
        if selected and not self.dirty:
            self.select_row(self.table.currentRow())

    @Slot(int, int, int, int)
    def select_row(self, row, *args):
        if self.pending or row < 0:
            return
        entity = self.snapshot[self.kind][row]
        self.selected_id = entity["id"]
        self.identity.setText(entity["id"])
        self.identity.setReadOnly(True)
        self.name.setText(entity["name"])
        self.aliases.setText("，".join(entity["aliases"]))
        self.enabled.setChecked(entity["enabled"])
        self.dirty = False

    def new(self):
        self.table.setEnabled(not self.pending)
        self.table.pager.setEnabled(not self.pending)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.selected_id = ""
        self.identity.setReadOnly(False)
        for field in (self.identity, self.name, self.aliases):
            field.clear()
        self.enabled.setChecked(True)
        self.dirty = False
        self.info.setText("填写新资料；ID保存后不改名，避免历史草稿指向其他对象。")

    def send(self, operation, entity):
        if self.pending:
            return
        self.pending = True
        self.set_controls(False)
        self.command_requested.emit(
            operation,
            dict(kind=self.kind, entity=entity, revision=self.snapshot["revision"]),
        )

    def save(self):
        identity = self.identity.text().strip()
        if not self.selected_id and any(
            row["id"] == identity for row in self.snapshot[self.kind]
        ):
            self.info.setText("该ID已存在，请选中对应记录后编辑，不能覆盖新增。")
            return
        aliases = self.aliases.text().replace("，", ",").replace("、", ",").split(",")
        entity = dict(
            id=identity,
            name=self.name.text().strip(),
            aliases=list(dict.fromkeys(s.strip() for s in aliases if s.strip())),
            enabled=self.enabled.isChecked(),
        )
        self.send("save", entity)

    def delete(self):
        if not self.selected_id:
            self.info.setText("请先选择要删除的资料；临时不用时可取消启用后保存。")
            return
        self.send("delete", {"id": self.selected_id})

    def refresh(self):
        self.send("query", {})

    def set_controls(self, enabled):
        for widget in (
            self.table,
            self.identity,
            self.name,
            self.aliases,
            self.enabled,
            *self.buttons.values(),
        ):
            widget.setEnabled(enabled)
        self.table.setEnabled(enabled and not self.dirty)
        self.table.pager.setEnabled(enabled and not self.dirty)

    def receive(self, value):
        addressed = value.get("kind") == self.kind
        if addressed:
            self.pending = False
            self.set_controls(True)
        if value.get("snapshot"):
            if self.dirty and not addressed:
                self.info.setText(
                    "基础资料有更新；保留未保存输入，保存前请刷新并核对。"
                )
                return
            self.snapshot = value["snapshot"]
            self.populate()
            if addressed and value.get("success"):
                self.new()
        if addressed:
            self.info.setText(value.get("message", "资料已更新"))
