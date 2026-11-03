"""可复用的卡片、状态标签和真实阶段指示。"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def label(text="", role="", wrap=False):
    item = QLabel(text)
    if role:
        item.setObjectName(role)
    item.setWordWrap(wrap)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    return item


def set_tone(widget, tone):
    widget.setProperty("tone", tone)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Badge(QLabel):
    def __init__(self, text="等待连接", tone="idle"):
        super().__init__(text)
        self.setObjectName("badge")
        self.setFixedHeight(27)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.update_status(text, tone)

    def update_status(self, text, tone="idle"):
        self.setText(text)
        set_tone(self, tone)


class Card(QFrame):
    def __init__(self, title, subtitle=""):
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(10)
        heading_widget = QWidget()
        heading_widget.setFixedHeight(28)
        self.heading = QHBoxLayout(heading_widget)
        self.heading.setContentsMargins(0, 0, 0, 0)
        self.heading.addWidget(label(title, "section"))
        self.heading.addStretch()
        self.body.addWidget(heading_widget)
        if subtitle:
            self.body.addWidget(label(subtitle, "muted", True))


class PipelineStrip(QFrame):
    """展示实际阶段，不根据计时推算识别进度百分比。"""

    def __init__(self):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.steps = []
        for index, name in enumerate(("音频接收", "语音识别", "语义解析", "结果保存")):
            value = label(f"{index + 1:02d}  {name}", "step")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(value)
            self.steps.append(value)

    def update_stage(self, stage, operation="", result_saved=False, error_code=""):
        active = {0: 0, 1: 1, 2: 1, 3: 2, 4: 2}.get(stage)
        for index, value in enumerate(self.steps):
            tone = "idle"
            if stage in (5, 6):
                tone = "good"
            elif active is not None:
                tone = (
                    "good"
                    if index < active
                    else "active" if index == active else "idle"
                )
            elif stage == 10:
                tone = "good" if index < 2 or index == 3 else "idle"
            elif stage in (7, 9):
                failed = (
                    2 if "parse" in operation else 1 if "recognize" in operation else 0
                )
                tone = (
                    "good" if index < failed else "bad" if index == failed else "idle"
                )
                if tone == "bad" and error_code == "UNSUPPORTED_TASK":
                    tone = "warn"
            if index == 3 and result_saved:
                tone = "good"
            set_tone(value, tone)


def glyph(kind, size):
    """绘制简洁线性图标，不将装饰图形冒充音量或波形。"""
    image = QPixmap(size, size)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 64, size / 64)
    painter.setPen(
        QPen(QColor("#2581fb"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    )
    if kind == "microphone":
        painter.drawRoundedRect(QRectF(25, 12, 14, 29), 7, 7)
        path = QPainterPath()
        path.moveTo(18, 32)
        path.cubicTo(18, 55, 46, 55, 46, 32)
        painter.drawPath(path)
        painter.drawLine(32, 49, 32, 56)
        painter.drawLine(24, 56, 40, 56)
    else:
        painter.drawRoundedRect(QRectF(12, 22, 40, 30), 7, 7)
        painter.drawLine(32, 12, 32, 22)
        painter.drawEllipse(QRectF(29, 7, 6, 6))
        painter.drawEllipse(QRectF(22, 32, 3, 3))
        painter.drawEllipse(QRectF(39, 32, 3, 3))
        painter.drawLine(25, 43, 39, 43)
        painter.drawLine(6, 31, 6, 43)
        painter.drawLine(58, 31, 58, 43)
    painter.end()
    return image
