"""在专用线程执行 ROS 与进程管理，通过 Qt Signal 传递界面数据。"""

import queue
import time
import traceback

import rclpy
from ai_edge.common.catalog import CatalogStore
from PySide6.QtCore import QThread, Signal
from rclpy.executors import SingleThreadedExecutor

from robot_visualization.ros.node import DashboardNode
from robot_visualization.runtime.console import console_log, format_log, log_level
from robot_visualization.runtime.supervisor import BackendSupervisor


class RosWorker(QThread):
    record = Signal(str, dict)
    availability = Signal(dict)
    control = Signal(dict)
    log = Signal(str)
    draft = Signal(dict)
    catalog = Signal(dict)

    def __init__(self):
        super().__init__()
        self.commands = queue.Queue(maxsize=16)
        self.node = DashboardNode(self)
        self.config = self.node.config
        self.manager = BackendSupervisor(self.config)
        self.catalog_store = CatalogStore(self.config.catalog_path)
        self.catalog_revision = self.config.catalog_snapshot["revision"]
        self.last_catalog_poll = 0.0
        self.catalog_error = ""

    def report_log(self, message):
        message = format_log(message, log_level(message))
        console_log(message)
        self.log.emit(message)
        self.manager.write_log(message)

    def command(self, operation, value=""):
        try:
            self.commands.put_nowait((operation, value))
        except queue.Full:
            self.report_log(f"未能排入操作：{operation}，操作队列已满")
            signal = (
                self.catalog
                if operation.startswith("catalog_")
                else self.draft if operation.startswith("draft_") else self.control
            )
            signal.emit(
                {
                    "success": False,
                    "message": "操作队列已满，请稍后再试",
                    "kind": value.get("kind") if isinstance(value, dict) else None,
                }
            )

    def catalog_command(self, operation, value):
        try:
            snapshot = (
                self.catalog_store.read()
                if operation == "query"
                else self.catalog_store.update(
                    operation, value["kind"], value["entity"], value["revision"]
                )
            )
            self.catalog_revision = snapshot["revision"]
            self.catalog.emit(
                dict(
                    success=True,
                    kind=value["kind"],
                    snapshot=snapshot,
                    message="基础资料已保存；同一资料库的新版后端在后续任务中使用",
                )
            )
            self.report_log(
                f"基础资料 {operation}：{value['kind']}；版本 {self.catalog_revision}"
            )
        except Exception as exc:
            self.report_log(f"基础资料 {operation} 失败：{exc}")
            self.catalog.emit(dict(success=False, kind=value["kind"], message=str(exc)))

    def poll_catalog(self):
        if time.monotonic() - self.last_catalog_poll < 1.0:
            return
        self.last_catalog_poll = time.monotonic()
        try:
            snapshot = self.catalog_store.read()
            self.catalog_error = ""
            if snapshot["revision"] != self.catalog_revision:
                self.catalog_revision = snapshot["revision"]
                self.catalog.emit(dict(success=True, snapshot=snapshot))
        except Exception as exc:
            if str(exc) != self.catalog_error:
                self.catalog_error = str(exc)
                self.report_log(f"基础资料刷新失败：{exc}")

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
                self.manager.executor_present = any(self.node.robot.provider_counts())
                if (
                    self.config.use_simulator
                    and self.manager.executor_present
                    and not {
                        "map",
                        "camera_raw",
                        "robot_state_node",
                        "execute_task_action_server",
                        "play_audio_action_server",
                    }
                    <= existing
                ):
                    self.node.robot.deployment_blocked = "模拟部署发现外部任务端点，但无法核实完整模拟进程组；禁止下发，请更换隔离 ROS_DOMAIN_ID"
                    self.report_log("[E] " + self.node.robot.deployment_blocked)
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
                        + (
                            f"，请求 ID：{value}"
                            if isinstance(value, str) and value
                            else ""
                        )
                    )
                    if operation.startswith("catalog_"):
                        self.catalog_command(operation.removeprefix("catalog_"), value)
                    elif self.node.simulation is not None and operation in (
                        "draft_simulate",
                        "draft_simulation_prepare",
                        "draft_simulation_cancel",
                        "draft_prepare_execution",
                        "draft_execute",
                        "draft_cancel_execution",
                        "draft_query_execution",
                    ):
                        if operation in (
                            "draft_simulation_prepare",
                            "draft_prepare_execution",
                        ):
                            self.node.simulation.prepare(value)
                        elif operation in ("draft_simulate", "draft_execute"):
                            self.node.simulation.execute(value)
                        elif operation == "draft_query_execution":
                            self.node.robot.query()
                        else:
                            self.node.simulation.cancel()
                    elif operation.startswith("draft_"):
                        self.node.drafts.send(operation.removeprefix("draft_"), value)
                    elif operation == "query":
                        self.node.request_query(value)
                    else:
                        self.node.request_control(operation)
                self.node.poll()
                self.poll_catalog()
                for line in self.manager.drain():
                    self.log.emit(line)
        except Exception as exc:
            self.report_log(
                format_log(f"工作台后端异常：{exc}", "ERROR")
                + "\n"
                + format_log(traceback.format_exc(), "DEBUG")
            )
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
