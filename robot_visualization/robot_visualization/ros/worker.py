"""在专用线程执行 ROS 与进程管理，通过 Qt Signal 传递界面数据。"""

import queue
import time
import traceback

import rclpy
from PySide6.QtCore import QThread, Signal
from rclpy.executors import SingleThreadedExecutor

from robot_visualization.ros.node import DashboardNode
from robot_visualization.runtime.console import console_log
from robot_visualization.runtime.supervisor import BackendSupervisor


class RosWorker(QThread):
    record = Signal(str, dict)
    availability = Signal(dict)
    control = Signal(dict)
    log = Signal(str)

    def __init__(self):
        super().__init__()
        self.commands = queue.Queue(maxsize=16)
        self.node = DashboardNode(self)
        self.config = self.node.config
        self.manager = BackendSupervisor(self.config)

    def report_log(self, message):
        console_log(message)
        self.log.emit(message)

    def command(self, operation, value=""):
        try:
            self.commands.put_nowait((operation, value))
        except queue.Full:
            self.report_log(f"未能排入操作：{operation}，操作队列已满")
            self.control.emit({"success": False, "message": "操作队列已满，请稍后再试"})

    def run(self):
        executor = SingleThreadedExecutor()
        try:
            executor.add_node(self.node)
            self.report_log("通信线程已启动，正在发现已有 ROS 节点与服务……")
            # 给 DDS 发现已有节点留出时间，避免重复创建用户已启动的进程。
            until = time.monotonic() + 1.5
            while time.monotonic() < until and not self.isInterruptionRequested():
                executor.spin_once(timeout_sec=0.05)
                self.node.poll()
            if not self.isInterruptionRequested():
                existing = set(self.node.get_node_names())
                self.report_log(
                    "已发现节点：" + ("、".join(sorted(existing)) or "暂无")
                )
                self.manager.start(existing)
                self.node.owned_nodes = set(self.manager.children)
            while rclpy.ok() and not self.isInterruptionRequested():
                executor.spin_once(timeout_sec=0.04)
                while not self.commands.empty():
                    operation, value = self.commands.get_nowait()
                    self.report_log(
                        f"处理界面操作：{operation}"
                        + (f"，请求 ID：{value}" if value else "")
                    )
                    if operation == "query":
                        self.node.request_query(value)
                    else:
                        self.node.request_control(operation)
                self.node.poll()
                for line in self.manager.drain():
                    self.log.emit(line)
        except Exception as exc:
            self.report_log(f"工作台后端异常：{exc}\n{traceback.format_exc()}")
        finally:
            self.cleanup(executor)

    def cleanup(self, executor):
        """任何一个清理步骤失败，都继续关闭本窗口拥有的节点及 ROS 资源。"""
        self.report_log("通信线程开始退出，正在停止录音并清理后台资源……")
        # 对外部设备，只结束本窗口明确发起的录音，不关闭外部进程。
        try:
            if (
                rclpy.ok()
                and self.node.started_recording
                and "microphone_sender_node" not in self.manager.children
            ):
                self.node.request_control("stop")
                until = time.monotonic() + 1.5
                while "control" in self.node.pending and time.monotonic() < until:
                    executor.spin_once(timeout_sec=0.05)
                    self.node.poll()
        except Exception as exc:
            self.report_log(f"停止外部录音时发生异常，继续清理：{exc}")
        steps = (
            ("后台进程组", self.manager.close),
            ("挂起的 ROS 请求", self.node.close),
            ("执行器节点", lambda: executor.remove_node(self.node)),
            ("ROS 节点", self.node.destroy_node),
            ("ROS 执行器", executor.shutdown),
        )
        for name, close in steps:
            try:
                close()
            except Exception as exc:
                self.report_log(f"清理{name}时发生异常，继续后续清理：{exc}")
        self.report_log("通信线程已退出")
