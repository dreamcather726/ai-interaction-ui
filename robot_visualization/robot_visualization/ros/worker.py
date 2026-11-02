"""在专用线程执行 ROS 与进程管理，通过 Qt Signal 传递界面数据。"""

import queue
import time

import rclpy
from PySide6.QtCore import QThread, Signal
from rclpy.executors import SingleThreadedExecutor

from robot_visualization.ros.node import DashboardNode
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

    def command(self, operation, value=""):
        try:
            self.commands.put_nowait((operation, value))
        except queue.Full:
            self.control.emit({"success": False, "message": "操作队列已满，请稍后再试"})

    def run(self):
        executor = SingleThreadedExecutor()
        executor.add_node(self.node)
        try:
            # 给 DDS 发现已有节点留出时间，避免重复创建用户已启动的进程。
            until = time.monotonic() + 1.5
            while time.monotonic() < until and not self.isInterruptionRequested():
                executor.spin_once(timeout_sec=0.05)
                self.node.poll()
            if not self.isInterruptionRequested():
                self.manager.start(set(self.node.get_node_names()))
                self.node.owned_nodes = set(self.manager.children)
            while rclpy.ok() and not self.isInterruptionRequested():
                executor.spin_once(timeout_sec=0.04)
                while not self.commands.empty():
                    operation, value = self.commands.get_nowait()
                    if operation == "query":
                        self.node.request_query(value)
                    else:
                        self.node.request_control(operation)
                self.node.poll()
                for line in self.manager.drain():
                    self.log.emit(line)
        except Exception as exc:
            self.log.emit(f"工作台后端异常：{exc}")
        finally:
            # 对外部设备，只结束本窗口明确发起的录音，不关闭外部进程。
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
            self.manager.close()
            self.node.close()
            executor.remove_node(self.node)
            self.node.destroy_node()
            executor.shutdown()
