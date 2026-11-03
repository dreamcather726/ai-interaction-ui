"""GUI-thread presentation for the opt-in simulator; no ROS imports or hardware I/O."""

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy


class SimulationImage(QLabel):
    def __init__(self, text):
        super().__init__(text)
        self.image = QImage()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(32, 32)

    def set_image(self, image):
        self.image = image.copy()  # Detach from the queued ROS message's byte buffer.
        self.refresh()

    def refresh(self):
        if not self.image.isNull() and self.isVisible():
            self.setPixmap(
                QPixmap.fromImage(self.image).scaled(
                    self.contentsRect().size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()


class SimulationView:
    def __init__(self, window):
        self.window = window
        self.map = None
        self.odom = None
        self.status = {}
        self.camera_size = None
        self.task = {}
        window.setWindowTitle(window.windowTitle() + " · SIM 模拟模式")
        window.system_badge.update_status("● SIM 模拟模式", "warn")
        window.draft_panel.buttons["confirm"].setText("确认并模拟执行")
        window.draft_panel.buttons["confirm"].setToolTip(
            "确认成功后调用专用模拟执行接口，不向真实机器人发送任务"
        )
        window.stage_progress.setFormat("模拟执行进度 %p%")
        window.cancel_simulation_button.clicked.connect(
            lambda: window.draft_requested.emit("simulation_cancel", {})
        )

    def receive(self, kind, value):
        window = self.window
        if kind == "sim_camera":
            width, height, stride = value["width"], value["height"], value["step"]
            if width > 0 and height > 0 and len(value["data"]) >= stride * height:
                image = QImage(
                    value["data"], width, height, stride, QImage.Format.Format_RGB888
                )
                for view in (window.camera_view, window.camera_page_view):
                    view.set_image(image)
                self.camera_size = (width, height)
                window.camera_page_info.setText(
                    f"SIM /camera/image_raw · {width} × {height} · rgb8"
                )
                self.render_camera_status()
        elif kind == "sim_map":
            width, height = value["width"], value["height"]
            if (
                width > 0
                and height > 0
                and len(value["data"]) == width * height
                and value["resolution"] > 0
            ):
                pixels = bytes(
                    160 if item < 0 else 245 - round(max(0, min(100, item)) * 2.15)
                    for item in value["data"]
                )
                image = QImage(
                    pixels, width, height, width, QImage.Format.Format_Grayscale8
                ).copy()
                self.map = (
                    value,
                    image.mirrored(False, True).convertToFormat(
                        QImage.Format.Format_RGB32
                    ),
                )
                self.paint_map()
        elif kind == "sim_odom":
            self.odom = value
            self.set_hardware_text(
                "机器人状态",
                f"SIM 位置 ({value['x']:.2f}, {value['y']:.2f}) m\n线速度 {value['speed']:.2f} m/s\n角速度 {value['angular']:.2f} rad/s",
            )
            self.render_chassis()
            self.paint_map()
        elif kind == "sim_battery":
            percentage, voltage = value["percentage"], value["voltage"]
            if math.isfinite(percentage) and math.isfinite(voltage):
                self.set_hardware_text(
                    "电池电量",
                    f"SIM 电量 {percentage * 100:.0f}%\n电压 {voltage:.2f} V",
                )
                window.robot_page_battery.setValue(
                    round(max(0, min(1, percentage)) * 100)
                )
                window.robot_page_battery.setFormat("SIM 电量 %p%")
        elif kind == "sim_status":
            self.status = value
            connected = "已连接" if value.get("network_connected") else "未连接"
            self.set_hardware_text(
                "网络连接",
                f"SIM 网络{connected}\nIP {value.get('ip', '—')}\n延迟 {value.get('network_latency_ms', '—')} ms",
            )
            self.render_chassis()
            self.render_camera_status()
        elif kind == "sim_task":
            if any(
                key in value and value[key] != self.task.get(key)
                for key in ("task_id", "draft_id", "draft_revision")
            ):
                self.task = {}
            self.task.update(value)
            self.render_task()

    def set_hardware_text(self, name, text):
        for fields in (self.window.hardware_labels, self.window.hardware_page_labels):
            if name in fields:
                fields[name].setText(text)

    def render_chassis(self):
        ready = "就绪" if self.status.get("chassis_ready") else "等待状态"
        speed = f"{self.odom['speed']:.2f} m/s" if self.odom else "等待里程计"
        mode = self.status.get("mode", "等待运行模式")
        self.set_hardware_text("底盘状态", f"SIM 底盘{ready}\n速度 {speed}\n{mode}")

    def render_camera_status(self):
        ready = "就绪" if self.status.get("camera_ready") else "等待状态"
        frame = (
            f"{self.camera_size[0]} × {self.camera_size[1]} · rgb8"
            if self.camera_size
            else "等待图像消息"
        )
        self.set_hardware_text("相机状态", f"SIM 相机{ready}\n{frame}")
        for view in (self.window.camera_view, self.window.camera_page_view):
            view.setToolTip(f"SIM 摄像头：{ready}；{frame}")

    def paint_map(self):
        if self.map is None:
            return
        metadata, original = self.map
        image = original.scaled(original.width() * 3, original.height() * 3)
        scale = 3 / metadata["resolution"]

        def point(x, y):
            return QPointF(
                (x - metadata["origin_x"]) * scale,
                image.height() - (y - metadata["origin_y"]) * scale,
            )

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#2479e8"), 2))
        for name, x in (("A", -2.0), ("B", 2.0)):
            location = point(x, 0)
            painter.setBrush(QColor("#d2e5ff"))
            painter.drawEllipse(location, 9, 9)
            painter.drawText(location + QPointF(12, -10), name)
        if self.odom:
            painter.setBrush(QColor("#2479e8"))
            painter.drawEllipse(point(self.odom["x"], self.odom["y"]), 7, 7)
        painter.end()
        for view in (self.window.map_view, self.window.map_page_view):
            view.set_image(image)
        position = (
            f"位置 ({self.odom['x']:.2f}, {self.odom['y']:.2f}) m"
            if self.odom
            else "等待 /odom 定位"
        )
        self.window.map_page_info.setText(
            f"SIM /map · {metadata['width']} × {metadata['height']} 栅格 · {metadata['resolution']:g} m/格 · {position}"
        )

    def render_task(self):
        task, window = self.task, self.window
        active, failed = task.get("active", False), task.get("failed", False)
        if "progress" in task:
            window.stage_progress.setValue(
                round(max(0, min(1, task["progress"])) * 100)
            )
        elif active or failed:
            window.stage_progress.setValue(0)
        window.stage_progress.setFormat("模拟执行进度 %p%")
        window.stage_badge.update_status(
            "模拟执行中" if active else "模拟失败/取消" if failed else "模拟执行完成",
            "bad" if failed else "active" if active else "good",
        )
        window.result_message.setPlainText(task.get("message", ""))
        window.request_label.setText("模拟任务：" + task.get("task_id", "准备中"))
        step = task.get("step", "准备中")
        window.simulation_step_label.setText("SIM 当前动作：" + step)
        window.task_information.setText(
            "SIM 任务 ID：" + task.get("task_id", "准备中") + "\n当前动作：" + step
        )
        self.set_hardware_text(
            "当前任务",
            "SIM "
            + ("执行中" if active else "已停止")
            + "\n"
            + task.get("task_id", "准备中")
            + "\n"
            + step,
        )
        window.task_badge.update_status(
            "● SIM 任务执行中" if active else "● SIM 任务已停止",
            "warn" if active else "good",
        )
        window.cancel_simulation_button.setEnabled(active)
        window.cancel_simulation_button.setText("取消模拟执行")
