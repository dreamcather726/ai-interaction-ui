"""GUI-thread presentation shared by real and simulated robots; no ROS I/O."""

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy
from robot_visualization.connection import RobotConnection
from robot_visualization.geometry import grid_point


class RobotImage(QLabel):
    def __init__(self, text):
        super().__init__(text)
        self.image = QImage()
        self.stale = False
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(32, 32)

    def set_image(self, image):
        self.image = image.copy()  # Detach from the queued ROS message's byte buffer.
        self.refresh()

    def refresh(self):
        if not self.image.isNull() and self.isVisible():
            pixmap = QPixmap.fromImage(self.image).scaled(
                self.contentsRect().size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            if self.stale and not pixmap.isNull():
                painter = QPainter(pixmap)
                painter.fillRect(0, 0, pixmap.width(), 30, QColor(35, 35, 35, 210))
                painter.setPen(QColor("white"))
                painter.drawText(8, 21, "画面已过期 · 最后一帧")
                painter.end()
            self.setPixmap(pixmap)

    def set_stale(self, stale):
        if self.stale != stale:
            self.stale = stale
            self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()


class RobotView:
    TELEMETRY_TIMEOUT = 2.0

    def __init__(self, window):
        self.window = window
        self.map = None
        self.odom = None
        self.status = {}
        self.camera_size = None
        self.camera_error = ""
        self.battery = None
        self.last_received = {}
        self.last_stamp = {}
        self.freshness = {}
        self.task = {}
        self.material = {}
        self.material_sequence = 0
        self.boot = None
        self.config = getattr(window.config, "robot", RobotConnection())
        self.TELEMETRY_TIMEOUT = self.config.telemetry_timeout
        self.prefix = "SIM " if getattr(window.config, "use_simulator", False) else ""
        window.system_badge.update_status(
            "● SIM 模拟部署" if self.prefix else "● 真实设备部署",
            "warn" if self.prefix else "idle",
        )
        window.draft_panel.buttons["confirm"].setText("确认并整单执行")
        window.draft_panel.buttons["confirm"].setToolTip(
            "先核对完整草稿，预检通过后确认；整份任务由执行端自主运行"
        )
        window.stage_progress.setFormat("执行进度 %p%")
        window.cancel_simulation_button.clicked.connect(
            lambda: window.draft_requested.emit("cancel_execution", {})
        )

    def touch(self, kind, value):
        stamp = value.get("stamp_ns")
        if stamp is not None:
            if stamp < self.last_stamp.get(kind, 0):
                return False
            if stamp == self.last_stamp.get(kind, 0):
                return kind == "robot_odom"  # TF may arrive after this same sample.
            self.last_stamp[kind] = stamp
        self.last_received[kind] = value.get("received_at", time.monotonic())
        return True

    def fresh(self, kind):
        received = self.last_received.get(kind)
        return (
            received is not None
            and 0 <= time.monotonic() - received < self.TELEMETRY_TIMEOUT
        )

    def position_fresh(self):
        return self.fresh("robot_odom")

    def check_freshness(self):
        current = {
            kind: self.fresh(kind)
            for kind in (
                "robot_camera",
                "robot_odom",
                "robot_battery",
                "robot_status",
                "robot_material",
            )
        }
        if current != self.freshness:
            self.freshness = current
            self.render_camera_status()
            self.render_position()
            self.render_chassis()
            self.render_battery()
            self.render_material_state()
            self.paint_map()

    def receive(self, kind, value):
        window = self.window
        kind = kind.replace("sim_", "robot_", 1)  # Legacy local event alias only.
        if kind.endswith("_error"):
            if kind == "robot_camera_error":
                self.camera_error = value["message"]
                window.camera_page_info.setText(value["message"])
                for view in (window.camera_view, window.camera_page_view):
                    view.setToolTip(value["message"])
                    view.set_stale(True)
            elif kind == "robot_map_error":
                self.map = None
                window.map_page_info.setText(value["message"])
                for view in (window.map_view, window.map_page_view):
                    view.set_stale(True)
            elif kind == "robot_material_error":
                self.material["known"] = False
                self.render_material_state()
            return
        if kind == "robot_heartbeat":
            if self.boot != value["boot_id"]:
                self.boot = value["boot_id"]
                self.material, self.material_sequence = {}, 0
                for key in ("robot_status", "robot_material", "robot_odom"):
                    self.last_received.pop(key, None)
                    self.last_stamp.pop(key, None)
            return
        if kind == "robot_camera":
            width, height, stride = value["width"], value["height"], value["step"]
            if width > 0 and height > 0 and len(value["data"]) >= stride * height:
                if not self.touch(kind, value):
                    return
                formats = {
                    "rgb8": QImage.Format.Format_RGB888,
                    "bgr8": QImage.Format.Format_BGR888,
                    "mono8": QImage.Format.Format_Grayscale8,
                    "rgba8": QImage.Format.Format_RGBA8888,
                }
                encoding = value.get("encoding", "rgb8")
                if encoding == "bgra8":
                    image = QImage(
                        value["data"],
                        width,
                        height,
                        stride,
                        QImage.Format.Format_RGBA8888,
                    ).rgbSwapped()
                elif encoding in formats:
                    image = QImage(
                        value["data"], width, height, stride, formats[encoding]
                    )
                else:
                    return
                for view in (window.camera_view, window.camera_page_view):
                    view.set_image(image)
                self.camera_size = (width, height)
                self.camera_error = ""
                self.camera_encoding = encoding
                window.camera_page_info.setText(
                    f"/camera/image_raw · {width} × {height} · rgb8"
                )
                self.render_camera_status()
        elif kind == "robot_map":
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
        elif kind == "robot_odom":
            if not all(
                math.isfinite(value[key]) for key in ("x", "y", "speed", "angular")
            ):
                return
            if not self.touch(kind, value):
                return
            self.odom = value
            self.render_position()
            self.render_chassis()
            self.paint_map()
        elif kind == "robot_battery":
            percentage = value["percentage"]
            if percentage is None or math.isfinite(percentage):
                if not self.touch(kind, value):
                    return
                self.battery = value
                self.render_battery()
        elif kind == "robot_status":
            if not self.touch(kind, value):
                return
            self.status = value
            window.simulation_scene_hint.setText(
                value.get("message", "执行端状态已更新")
            )
            self.render_position()
            self.render_chassis()
            self.render_camera_status()
            self.render_material_state()
            self.paint_map()
        elif kind == "robot_material":
            if (
                value.get("boot_id") != self.boot
                or value.get("event_seq", 0) <= self.material_sequence
            ):
                return
            self.material_sequence = value["event_seq"]
            if not self.touch(kind, value):
                return
            self.material = value
            self.render_material_state()
            self.paint_map()
        elif kind == "robot_task":
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
        status_fresh = self.fresh("robot_status")
        ready = (
            "就绪"
            if status_fresh and self.status.get("chassis_ready")
            else "状态未确认"
        )
        speed = (
            f"{self.odom['speed']:.2f} m/s"
            if self.position_fresh()
            else "未知（位置未更新）"
        )
        mode = (
            self.status.get("mode", "等待运行模式")
            if status_fresh
            else "状态已过期／等待更新"
        )
        self.set_hardware_text("底盘状态", f"底盘{ready}\n速度 {speed}\n{mode}")

    def render_position(self):
        if self.odom is None:
            self.set_hardware_text("机器人状态", "等待定位；速度未确认")
        elif self.position_fresh():
            value = self.odom
            self.set_hardware_text(
                "机器人状态",
                f"{value.get('frame_id', '未知坐标系')} 位置 ({value['x']:.2f}, {value['y']:.2f}) m\n线速度 {value['speed']:.2f} m/s\n角速度 {value['angular']:.2f} rad/s",
            )
        else:
            self.set_hardware_text(
                "机器人状态",
                f"定位已过期\n最后位置 ({self.odom['x']:.2f}, {self.odom['y']:.2f}) m\n速度未确认",
            )

    def render_battery(self):
        bar = self.window.robot_page_battery
        fresh = self.fresh("robot_battery")
        bar.setEnabled(fresh)
        if self.battery is None:
            self.set_hardware_text("电池电量", "等待电池数据")
            bar.setFormat("电量未确认")
            return
        value = self.battery
        self.set_hardware_text(
            "电池电量",
            ("" if fresh else "数据已过期，最后记录：\n")
            + (
                f"电量 {value['percentage'] * 100:.0f}%"
                if value["percentage"] is not None
                else "电量未知"
            )
            + (
                f"\n电压 {value['voltage']:.2f} V"
                if value["voltage"] is not None
                else "\n电压未知"
            ),
        )
        if value["percentage"] is not None:
            bar.setValue(round(max(0, min(1, value["percentage"])) * 100))
        bar.setEnabled(fresh and value["percentage"] is not None)
        bar.setFormat(
            "电量未知"
            if value["percentage"] is None
            else "电量 %p%" if fresh else "电量已过期 · 最后 %p%"
        )

    def render_camera_status(self):
        fresh = self.fresh("robot_camera")
        if self.camera_error:
            self.set_hardware_text("相机状态", self.camera_error)
            self.window.camera_page_info.setText(self.camera_error)
            for view in (self.window.camera_view, self.window.camera_page_view):
                view.setToolTip(self.camera_error)
                view.set_stale(True)
            return
        ready = "就绪" if fresh else "画面已过期" if self.camera_size else "等待图像"
        frame = (
            f"{self.camera_size[0]} × {self.camera_size[1]} · {getattr(self, 'camera_encoding', 'rgb8')}"
            if self.camera_size
            else "等待图像消息"
        )
        self.set_hardware_text("相机状态", f"相机{ready}\n{frame}")
        self.window.camera_page_info.setText(
            f"{self.config.camera_topic} · {ready} · {frame}"
        )
        for view in (self.window.camera_view, self.window.camera_page_view):
            view.setToolTip(f"摄像头：{ready}；{frame}")
            view.set_stale(not fresh)

    def render_material_state(self):
        field = self.window.material_state_label
        if not self.fresh("robot_material") or not self.material.get("known"):
            field.setText("持有物料：状态未确认")
            field.setToolTip("状态缺失或已过期，不能据此判断空手或持物")
            return
        held = self.material["held_object_id"]
        field.setText("持有物料：" + (held or "无"))
        details = [
            f"{item['id']}："
            + (
                "随机器人携带"
                if item.get("carried")
                else item.get("station_id", "未知位置")
            )
            for item in self.material.get("objects", [])
            if isinstance(item, dict) and "id" in item
        ]
        field.setToolTip("\n".join(details) or "物料清单为空")

    def paint_map(self):
        if self.map is None:
            return
        metadata, original = self.map
        image = original.scaled(original.width() * 3, original.height() * 3)

        def point(x, y):
            gx, gy = grid_point(
                x,
                y,
                metadata["origin_x"],
                metadata["origin_y"],
                metadata.get("origin_yaw", 0.0),
                metadata["resolution"],
            )
            return QPointF(gx * 3, image.height() - gy * 3)

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#2479e8"), 2))
        if (
            self.odom
            and self.odom.get("map_valid")
            and self.odom.get("map_frame") == metadata.get("frame_id")
        ):
            painter.setBrush(QColor("#2479e8" if self.position_fresh() else "#999999"))
            painter.drawEllipse(point(self.odom["map_x"], self.odom["map_y"]), 7, 7)
        for item in self.material.get("objects", []):
            if (
                not isinstance(item, dict)
                or not all(key in item for key in ("id", "x", "y"))
                or not item.get("position_known")
                or self.material.get("frame_id") != metadata.get("frame_id")
            ):
                continue
            location = point(item["x"], item["y"]) + QPointF(0, 16)
            material_fresh = self.fresh("robot_material") and self.material.get("known")
            color = "#d69827" if item.get("carried") else "#39a278"
            painter.setBrush(QColor(color if material_fresh else "#999999"))
            painter.drawRect(QRectF(location.x() - 4, location.y() - 4, 8, 8))
            painter.drawText(location + QPointF(8, 6), str(item["id"]))
        painter.end()
        for view in (self.window.map_view, self.window.map_page_view):
            view.set_stale(False)
            view.set_image(image)
        position = (
            (
                ("地图位置" if self.position_fresh() else "地图定位已过期")
                + f" ({self.odom['map_x']:.2f}, {self.odom['map_y']:.2f}) m"
            )
            if self.odom and self.odom.get("map_valid")
            else (
                self.odom.get("position_message", "地图定位未确认")
                if self.odom
                else "等待定位与 TF"
            )
        )
        self.window.map_page_info.setText(
            f"{self.config.map_topic} [{metadata.get('frame_id', '未知frame')}] · {metadata['width']} × {metadata['height']} 栅格 · {metadata['resolution']:g} m/格 · {position}"
        )

    def render_task(self):
        task, window = self.task, self.window
        active, failed = task.get("active", False), task.get("failed", False)
        unknown, canceling = task.get("execution_unknown", False), task.get(
            "canceling", False
        )
        progress = task.get("progress", -1.0)
        valid_progress = (
            isinstance(progress, (float, int))
            and math.isfinite(progress)
            and 0 <= progress <= 1
        )
        if valid_progress:
            window.stage_progress.setValue(round(progress * 100))
        window.stage_progress.setFormat(
            "执行进度 %p%" if valid_progress else "执行进度未知"
        )
        if unknown:
            title, summary, tone = "执行结果未知", "结果未知", "warn"
        elif active and canceling:
            title, summary, tone = "等待停止确认", "等待停止确认", "warn"
        elif task.get("preparation_pending"):
            title, summary, tone = "正在预检草稿", "预检中，尚未执行", "active"
        elif task.get("prepared"):
            title, summary, tone = "预检通过，等待确认", "待你确认整单执行", "good"
        elif active:
            title, summary, tone = "执行中", "任务执行中", "active"
        elif failed and task.get("retryable"):
            title, summary, tone = "预检未通过", "尚未执行，等待处理", "warn"
        elif failed:
            title, summary, tone = "失败/取消", "任务失败／取消", "bad"
        elif task.get("state") == 3:
            title, summary, tone = "执行完成", "任务已完成", "good"
        else:
            title, summary, tone = "等待执行", "等待执行", "idle"
        window.stage_badge.update_status(title, tone)
        window.result_message.setPlainText(task.get("message", ""))
        window.request_label.setText("任务：" + task.get("task_id", "准备中"))
        step = task.get("step", "准备中")
        window.simulation_step_label.setText("当前动作：" + step)
        window.task_information.setText(
            "任务 ID："
            + task.get("task_id", "准备中")
            + "\n下发 ID："
            + task.get("dispatch_id", "准备中")
            + "\n当前动作："
            + step
        )
        self.set_hardware_text(
            "当前任务",
            "" + summary + "\n" + task.get("task_id", "准备中") + "\n" + step,
        )
        window.task_badge.update_status("● " + self.prefix + summary, tone)
        window.cancel_simulation_button.setEnabled(
            (
                (active and task.get("cancel_available", True))
                or (unknown and task.get("cancel_available", False))
            )
            and not canceling
        )
        window.cancel_simulation_button.setText(
            "等待停止确认"
            if canceling
            else "取消预检" if task.get("preparation_pending") else "取消执行"
        )


# Compatibility imports for existing integrations.
SimulationImage = RobotImage
SimulationView = RobotView
