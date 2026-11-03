"""无滚动展示组件：内容完整保留，使用按钮翻页。"""

import math
from html import escape

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QTextLayout, QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from robot_visualization.runtime.console import log_level


class NoWheel(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel:
            event.accept()
            return True
        return super().eventFilter(watched, event)


def disable_scrolling(widget):
    widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    widget.setAutoScroll(False)
    widget._no_wheel = NoWheel(widget)
    widget.viewport().installEventFilter(widget._no_wheel)


class PageBar(QWidget):
    changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.page, self.pages = 0, 1
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.previous = QPushButton("上一页")
        self.next = QPushButton("下一页")
        self.counter = QLabel()
        self.counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for button in (self.previous, self.next):
            button.setObjectName("small")
        row.addStretch()
        row.addWidget(self.previous)
        row.addWidget(self.counter)
        row.addWidget(self.next)
        row.addStretch()
        self.previous.clicked.connect(lambda: self.go(self.page - 1))
        self.next.clicked.connect(lambda: self.go(self.page + 1))
        self.set_pages(1)

    def set_pages(self, pages, page=None):
        self.pages = max(1, pages)
        self.page = min(max(0, self.page if page is None else page), self.pages - 1)
        self.counter.setText(f"第 {self.page + 1} / {self.pages} 页")
        self.previous.setEnabled(self.page > 0)
        self.next.setEnabled(self.page + 1 < self.pages)

    def go(self, page):
        previous = self.page
        self.set_pages(self.pages, page)
        if previous != self.page:
            self.changed.emit(self.page)


class PageTabs(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUsesScrollButtons(False)
        self.tabBar().setExpanding(True)
        self._no_wheel = NoWheel(self)
        self.tabBar().installEventFilter(self._no_wheel)


class ElidedLabel(QLabel):
    """单行提示；完整内容仍可从 text() 和悬停提示读取。"""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, text):
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._render()

    def text(self):
        return self._full_text

    def clear(self):
        self.setText("")

    def _render(self):
        super().setText(
            self.fontMetrics().elidedText(
                self._full_text.replace("\n", " "),
                Qt.TextElideMode.ElideMiddle,
                max(1, self.width() - 4),
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()


class PagedText(QWidget):
    """按实际宽高换行分页，原文不截断；不使用文本滚动控件。"""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._text = str(text)
        self._lines = []
        self._line_limit = 0
        self._rows = 1
        self._follow_tail = False
        self.body = QLabel()
        self.body.setObjectName("pagedText")
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.body.setContentsMargins(8, 6, 8, 6)
        self.pager = PageBar()
        self.pager.changed.connect(self._render)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.body, 1)
        layout.addWidget(self.pager)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._reflow)
        self._timer.start(0)

    def setPlainText(self, text):
        text = str(text)
        if text == self._text:
            return
        self._text = text
        self._follow_tail = False
        self.pager.set_pages(1, 0)
        self._timer.start(0)

    def setText(self, text):
        self.setPlainText(text)

    def text(self):
        return self._text

    def toPlainText(self):
        return self._text

    def clear(self):
        self.setPlainText("")

    def setMaximumBlockCount(self, count):
        self._line_limit = max(0, count)

    def appendPlainText(self, text):
        self._follow_tail = self._follow_tail or self.pager.page == self.pager.pages - 1
        self._text += ("\n" if self._text else "") + str(text)
        if self._line_limit:
            self._text = "\n".join(self._text.split("\n")[-self._line_limit :])
        self._timer.start(0)

    def _reflow(self):
        if not self.isVisible():
            return
        metrics = self.body.fontMetrics()
        actual_width = self.body.contentsRect().width() - 2
        width = actual_width if actual_width >= 16 else 400
        self._rows = max(
            1, self.body.contentsRect().height() // max(1, metrics.lineSpacing())
        )
        self._lines = []
        row_height = metrics.lineSpacing()
        for paragraph in self._text.replace("\t", "    ").split("\n"):
            if not paragraph:
                self._lines.append("")
                continue
            text_layout = QTextLayout(paragraph, self.body.font())
            option = QTextOption()
            option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            text_layout.setTextOption(option)
            text_layout.beginLayout()
            # QTextLine 的偏移是 UTF-16 单元，按相同编码切片，避免表情后文字丢失。
            encoded = paragraph.encode("utf-16-le")
            while True:
                line = text_layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(width)
                row_height = max(row_height, math.ceil(line.height()))
                start = line.textStart() * 2
                end = start + line.textLength() * 2
                self._lines.append(encoded[start:end].decode("utf-16-le"))
            text_layout.endLayout()
        self._rows = max(1, self.body.contentsRect().height() // max(1, row_height))
        pages = max(1, math.ceil(len(self._lines) / self._rows))
        self.pager.set_pages(pages, pages - 1 if self._follow_tail else None)
        self._follow_tail = False
        self._render()

    def _render(self, *args):
        start = self.pager.page * self._rows
        self.body.setText("\n".join(self._lines[start : start + self._rows]))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._timer.start(0)

    def showEvent(self, event):
        super().showEvent(event)
        self._reflow()


class PagedLog(PagedText):
    """日志专用着色视图；只渲染转义后的可见页，不引入滚动控件。"""

    COLORS = {
        "ERROR": "#c85648",
        "WARNING": "#ac7c23",
        "INFO": "#29486d",
        "DEBUG": "#8797ad",
    }

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.body.setTextFormat(Qt.TextFormat.RichText)

    def _render(self, *args):
        start = self.pager.page * self._rows
        fragments = []
        level = "INFO"
        for index, line in enumerate(self._lines):
            if line.startswith(("[D]", "[I]", "[W]", "[E]")):
                level = log_level(line)
            if start <= index < start + self._rows:
                content = escape(line).replace(" ", "&nbsp;") or "&nbsp;"
                fragments.append(
                    f'<span style="color:{self.COLORS[level]}">{content}</span>'
                )
        self.body.setText("<br/>".join(fragments))


class PagedTable(QTableWidget):
    """保留全部可编辑行，分页隐藏行；业务读取仍能取得完整列表。"""

    def __init__(self, columns, parent=None):
        super().__init__(0, columns, parent)
        self.pager = PageBar()
        self.page_size = 1
        self.keep_empty_selection = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        disable_scrolling(self)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._reflow)
        self.pager.changed.connect(self._page_changed)
        self.currentCellChanged.connect(self._follow_selection)

    def _schedule(self):
        if hasattr(self, "_timer"):
            self._timer.start(0)

    def setRowCount(self, count):
        super().setRowCount(count)
        self._schedule()

    def insertRow(self, row):
        super().insertRow(row)
        self._schedule()

    def removeRow(self, row):
        super().removeRow(row)
        self._schedule()

    def _reflow(self):
        height = max(32, self.fontMetrics().lineSpacing() + 16)
        self.verticalHeader().setDefaultSectionSize(height)
        self.page_size = max(1, (self.viewport().height() - 4) // height)
        selected = self.currentRow()
        self.pager.set_pages(
            math.ceil(self.rowCount() / self.page_size),
            selected // self.page_size if selected >= 0 else None,
        )
        self._render()

    def _render(self, *args):
        start = self.pager.page * self.page_size
        for row in range(self.rowCount()):
            self.setRowHidden(row, not start <= row < start + self.page_size)
        if (
            self.rowCount()
            and (self.currentRow() >= 0 or not self.keep_empty_selection)
            and not start <= self.currentRow() < start + self.page_size
        ):
            self.setCurrentCell(start, 0)

    def show_row(self, row):
        if row >= 0:
            self.pager.set_pages(
                math.ceil(self.rowCount() / self.page_size), row // self.page_size
            )
            self._render()

    def _page_changed(self, page):
        self._render()
        if self.rowCount():
            self.setCurrentCell(page * self.page_size, 0)

    def _follow_selection(self, row, *args):
        # 等选择回调返回再隐藏行，避免在 Qt 的选择更新内部修改视图几何。
        self._schedule()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            direction = -1 if event.key() == Qt.Key.Key_PageUp else 1
            self.pager.go(self.pager.page + direction)
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule()

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule()


class PagedList(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pager = PageBar()
        self.page_size = 1
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        disable_scrolling(self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh_pages)
        self.pager.changed.connect(self._page_changed)
        self.currentRowChanged.connect(self._follow_selection)

    def refresh_pages(self):
        height = max(40, self.fontMetrics().lineSpacing() * 2 + 26)
        for index in range(self.count()):
            self.item(index).setSizeHint(QSize(0, height))
        self.page_size = max(1, (self.viewport().height() - 4) // height)
        selected = self.currentRow()
        self.pager.set_pages(
            math.ceil(self.count() / self.page_size),
            selected // self.page_size if selected >= 0 else None,
        )
        self._render()

    def _render(self):
        start = self.pager.page * self.page_size
        for index in range(self.count()):
            self.item(index).setHidden(not start <= index < start + self.page_size)
        if self.count() and not start <= self.currentRow() < start + self.page_size:
            self.setCurrentRow(start)

    def _page_changed(self, page):
        self._render()
        if self.count():
            self.setCurrentRow(page * self.page_size)

    def _follow_selection(self, row):
        if row >= 0:
            self._timer.start(0)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            direction = -1 if event.key() == Qt.Key.Key_PageUp else 1
            self.pager.go(self.pager.page + direction)
            event.accept()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_pages()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_pages()


class PagedCombo(QWidget):
    """草稿列表每组最多五项，弹出菜单也不需要滚动。"""

    activated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self._current = -1
        self._visible_items = None
        self.box = QComboBox()
        self.box.setMaxVisibleItems(5)
        self.box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.box.setMinimumContentsLength(12)
        self.box.view().setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.box.view().setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._no_wheel = NoWheel(self)
        self.box.installEventFilter(self._no_wheel)
        self.box.view().viewport().installEventFilter(self._no_wheel)
        self.pager = PageBar()
        self.pager.changed.connect(self._page_changed)
        self.box.activated.connect(self._activate)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.box, 1)
        row.addWidget(self.pager)

    def setPlaceholderText(self, text):
        self.box.setPlaceholderText(text)

    def count(self):
        return len(self._items)

    def clear(self):
        self._items.clear()
        self._current = -1
        self._visible_items = None
        self.pager.set_pages(1, 0)
        self.box.clear()

    def addItem(self, text, value):
        self._items.append((text, value))
        self.pager.set_pages(math.ceil(len(self._items) / 5))
        self._render()

    def itemData(self, index):
        return self._items[index][1] if 0 <= index < len(self._items) else None

    def findData(self, value):
        return next(
            (index for index, item in enumerate(self._items) if item[1] == value), -1
        )

    def setCurrentIndex(self, index):
        self._current = index if 0 <= index < len(self._items) else -1
        self.pager.set_pages(
            math.ceil(len(self._items) / 5), index // 5 if index >= 0 else None
        )
        self._render()

    def _render(self):
        start = self.pager.page * 5
        visible = self._items[start : start + 5]
        blocker = QSignalBlocker(self.box)
        if visible != self._visible_items:
            self.box.clear()
            for title, value in visible:
                self.box.addItem(title, value)
                self.box.setItemData(
                    self.box.count() - 1, title, Qt.ItemDataRole.ToolTipRole
                )
            self._visible_items = list(visible)
        self.box.setCurrentIndex(
            self._current - start if start <= self._current < start + 5 else -1
        )
        blocker.unblock()

    def _activate(self, index):
        self._current = self.pager.page * 5 + index
        self.activated.emit(self._current)

    def _page_changed(self, page):
        # 浏览另一组不会选择或加载草稿；真实选择仅由下拉框 activated 发出。
        self._render()
