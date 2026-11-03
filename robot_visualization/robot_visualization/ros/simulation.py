"""Opt-in telemetry and simulation-only dispatch; never calls a real task server."""

import json
import time
from uuid import uuid4

from ai_edge.semantic.ros_candidates import assign_candidate
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.action import ActionClient
from rclpy.qos import (
    DurabilityPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from robot_interfaces.action import ExecuteTask
from robot_interfaces.srv import PrepareTask
from sensor_msgs.msg import BatteryState, Image
from std_msgs.msg import String

from robot_visualization.task_model import normalize_candidate


class SimulationClient:
    def __init__(self, node, events):
        self.node, self.events = node, events
        self.prepare_client = node.create_client(PrepareTask, "/simulator/task/prepare")
        self.executor = ActionClient(node, ExecuteTask, "/simulator/task/execute")
        self.pending = None
        self.handle = None
        self.goal = None
        self.cancel_requested = False
        self.dispatched = set()
        self.source_draft = {}
        self.subscriptions = [
            node.create_subscription(
                OccupancyGrid,
                "/map",
                self.on_map,
                QoSProfile(
                    depth=1,
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                ),
            ),
            node.create_subscription(
                Image, "/camera/image_raw", self.on_camera, qos_profile_sensor_data
            ),
            node.create_subscription(
                Odometry, "/odom", self.on_odom, qos_profile_sensor_data
            ),
            node.create_subscription(
                BatteryState, "/battery", self.on_battery, qos_profile_sensor_data
            ),
            node.create_subscription(String, "/robot_status", self.on_status, 10),
        ]

    def on_map(self, message):
        self.events.record.emit(
            "sim_map",
            {
                "width": message.info.width,
                "height": message.info.height,
                "resolution": message.info.resolution,
                "origin_x": message.info.origin.position.x,
                "origin_y": message.info.origin.position.y,
                "data": list(message.data),
            },
        )

    def on_camera(self, message):
        if message.encoding == "rgb8" and message.step >= message.width * 3:
            self.events.record.emit(
                "sim_camera",
                {
                    "width": message.width,
                    "height": message.height,
                    "step": message.step,
                    "data": bytes(message.data),
                },
            )

    def on_odom(self, message):
        self.events.record.emit(
            "sim_odom",
            {
                "x": message.pose.pose.position.x,
                "y": message.pose.pose.position.y,
                "speed": message.twist.twist.linear.x,
                "angular": message.twist.twist.angular.z,
            },
        )

    def on_battery(self, message):
        self.events.record.emit(
            "sim_battery",
            {"percentage": message.percentage, "voltage": message.voltage},
        )

    def on_status(self, message):
        try:
            value = json.loads(message.data)
            if isinstance(value, dict) and value.get("simulator") is True:
                self.events.record.emit("sim_status", value)
        except (ValueError, TypeError):
            pass

    def emit_task(self, value):
        self.events.record.emit("sim_task", dict(value, **self.source_draft))

    def announce(self, message, *, failed=False):
        self.events.report_log(("[E] " if failed else "[I] ") + "[SIM] " + message)
        value = {
            "task_id": self.goal.task_id if self.goal else "",
            "message": message,
            "failed": failed,
            "active": self.pending is not None,
        }
        if failed and self.pending is None:
            value["state"] = 4
        self.emit_task(value)

    def execute(self, draft):
        if self.pending is not None:
            self.announce("已有模拟任务正在处理，不重复提交", failed=True)
            return
        key = (draft.get("draft_id"), draft.get("revision"))
        if key in self.dispatched:
            self.announce(
                "此版本已提交过模拟，不自动重放；修改并保存新版本后再确认", failed=True
            )
            return
        self.source_draft = {"draft_id": key[0], "draft_revision": key[1]}
        self.goal = None
        try:
            if draft.get("state") != 1:
                raise ValueError("只能模拟执行明确确认的草稿")
            if (
                not self.prepare_client.service_is_ready()
                or not self.executor.server_is_ready()
            ):
                raise ValueError(
                    "模拟执行服务未就绪，请启动 simulator_all 后重新确认草稿"
                )
            request = PrepareTask.Request()
            request.stamp = self.node.get_clock().now().to_msg()
            request.request_id = "sim-prepare-" + uuid4().hex
            request.confirmation_context_id = "sim-confirm-" + uuid4().hex
            request.task.stamp = request.stamp
            request.task.interface_version = "0.3.1"
            request.task.task_id = "sim-task-" + uuid4().hex
            request.task.source_session_id = draft["session_id"]
            request.task.source_input_id = draft["draft_id"]
            request.task.source_context_id = draft["input_context_id"]
            assign_candidate(
                request.task.candidate,
                normalize_candidate(draft["candidate"]),
                request.stamp,
            )
            self.goal = ExecuteTask.Goal()
            self.goal.request_id = "sim-execute-" + uuid4().hex
            self.goal.task_id = request.task.task_id
            self.goal.dispatch_id = "sim-dispatch-" + uuid4().hex
            self.goal.session_id = draft["session_id"]
            self.goal.input_context_id = request.confirmation_context_id
            self.cancel_requested = False
            self.pending = (
                "prepare",
                self.prepare_client.call_async(request),
                time.monotonic() + 5.0,
                request.request_id,
            )
            self.dispatched.add(key)
            self.emit_task(
                {
                    "task_id": self.goal.task_id,
                    "progress": 0.0,
                    "step": "",
                    "active": True,
                    "failed": False,
                }
            )
            self.announce("已确认草稿，正在准备模拟执行；不会调用真实 /task/execute")
        except (ValueError, KeyError, TypeError) as exc:
            self.announce(str(exc), failed=True)

    def on_feedback(self, message):
        feedback = message.feedback
        if (
            self.pending is not None
            and self.goal
            and (feedback.task_id, feedback.dispatch_id)
            == (
                self.goal.task_id,
                self.goal.dispatch_id,
            )
        ):
            self.emit_task(
                {
                    "task_id": feedback.task_id,
                    "message": feedback.message,
                    "step": feedback.task.step,
                    "progress": feedback.task.progress,
                    "active": True,
                    "state": feedback.task.state,
                    "failed": False,
                },
            )

    def poll(self):
        if self.pending is None:
            return
        phase, future, deadline, request_id = self.pending
        if not future.done() and time.monotonic() < deadline:
            return
        try:
            if not future.done():
                if phase == "prepare":
                    self.prepare_client.remove_pending_request(future)
                    future.cancel()
                elif phase == "goal":
                    future.add_done_callback(self.cancel_late_goal)
                raise TimeoutError("模拟请求响应超时，结果未知；不会自动重试")
            response = future.result()
            if phase == "prepare":
                if self.cancel_requested:
                    self.pending = None
                    message = "模拟准备已取消，未发送执行 Goal"
                    self.emit_task(
                        {
                            "task_id": self.goal.task_id,
                            "message": message,
                            "progress": 0.0,
                            "active": False,
                            "failed": True,
                            "state": 5,
                            "error_code": "CANCELED",
                        }
                    )
                    self.events.report_log("[I] [SIM] " + message)
                    return
                if (
                    not response.success
                    or response.request_id != request_id
                    or response.task.task_id != self.goal.task_id
                    or response.confirmation_context_id != self.goal.input_context_id
                ):
                    raise ValueError(response.message or "模拟准备结果不匹配")
                self.goal.stamp = self.node.get_clock().now().to_msg()
                self.goal.confirmed_at = self.goal.stamp
                self.goal.revision = response.revision
                self.pending = (
                    "goal",
                    self.executor.send_goal_async(
                        self.goal, feedback_callback=self.on_feedback
                    ),
                    time.monotonic() + 5.0,
                    self.goal.request_id,
                )
            elif phase == "goal":
                if not response.accepted:
                    raise ValueError("模拟执行 Goal 被拒绝；不自动重发")
                self.handle = response
                self.pending = (
                    "result",
                    response.get_result_async(),
                    float("inf"),
                    request_id,
                )
                if self.cancel_requested:
                    self.handle.cancel_goal_async()
            else:
                result = response.result
                if (result.request_id, result.task_id, result.dispatch_id) != (
                    request_id,
                    self.goal.task_id,
                    self.goal.dispatch_id,
                ):
                    raise ValueError("模拟执行结果与当前任务不匹配")
                self.pending, self.handle = None, None
                self.emit_task(
                    {
                        "task_id": result.task_id,
                        "message": result.message,
                        "step": result.task.step,
                        "progress": result.task.progress,
                        "active": False,
                        "state": result.task.state,
                        "failed": not result.success,
                        "error_code": result.error_code,
                    },
                )
                self.events.report_log(
                    ("[I] " if result.success else "[E] ") + "[SIM] " + result.message
                )
        except Exception as exc:
            self.pending = None
            if self.handle is not None:
                self.handle.cancel_goal_async()
                self.handle = None
            self.announce(str(exc), failed=True)

    @staticmethod
    def cancel_late_goal(future):
        try:
            handle = future.result()
            if handle.accepted:
                handle.cancel_goal_async()
        except Exception:
            pass

    def cancel(self):
        self.cancel_requested = True
        if self.handle is not None:
            self.handle.cancel_goal_async()
        if self.pending is not None:
            self.announce("已请求取消模拟任务，等待服务端确认停止")

    def close(self):
        self.cancel()
        if self.pending is not None:
            phase, future, _, _ = self.pending
            if phase == "goal":
                future.add_done_callback(self.cancel_late_goal)
            elif phase == "prepare":
                self.prepare_client.remove_pending_request(future)
                future.cancel()
        self.executor.destroy()
