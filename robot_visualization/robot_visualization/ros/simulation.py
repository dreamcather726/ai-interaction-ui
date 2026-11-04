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
    RESPONSE_TIMEOUT = 5.0
    FEEDBACK_TIMEOUT = 5.0
    CANCEL_TIMEOUT = 5.0

    def __init__(self, node, events):
        self.node, self.events = node, events
        self.prepare_client = node.create_client(PrepareTask, "/simulator/task/prepare")
        self.executor = ActionClient(node, ExecuteTask, "/simulator/task/execute")
        self.pending = None
        self.handle = None
        self.goal = None
        self.cancel_requested = False
        self.cancel_future = None
        self.cancel_deadline = None
        self.uncertain = False
        self.unknown_message = ""
        self.last_feedback_seq = 0
        self.attempt_key = None
        self.prepare_request = None
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
        if (
            message.encoding == "rgb8"
            and message.width > 0
            and message.height > 0
            and message.step >= message.width * 3
            and len(message.data) >= message.step * message.height
        ):
            self.events.record.emit(
                "sim_camera",
                {
                    "width": message.width,
                    "height": message.height,
                    "step": message.step,
                    "data": bytes(message.data),
                    **self.sample_time(message.header.stamp),
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
                **self.sample_time(message.header.stamp),
            },
        )

    def on_battery(self, message):
        self.events.record.emit(
            "sim_battery",
            {
                "percentage": message.percentage,
                "voltage": message.voltage,
                **self.sample_time(message.header.stamp),
            },
        )

    @staticmethod
    def sample_time(stamp):
        return {
            "received_at": time.monotonic(),
            "stamp_ns": stamp.sec * 10**9 + stamp.nanosec,
        }

    def on_status(self, message):
        try:
            value = json.loads(message.data)
            if isinstance(value, dict) and value.get("simulator") is True:
                value["received_at"] = time.monotonic()
                stamp = value.get("stamp", {})
                value["stamp_ns"] = int(stamp["sec"]) * 10**9 + int(stamp["nanosec"])
                self.events.record.emit("sim_status", value)
        except (ValueError, TypeError, KeyError):
            pass

    def emit_task(self, value):
        self.events.record.emit(
            "sim_task",
            dict(
                value,
                dispatch_id=self.goal.dispatch_id if self.goal else "",
                **self.source_draft,
            ),
        )

    def announce(self, message, *, failed=False):
        self.events.report_log(("[E] " if failed else "[I] ") + "[SIM] " + message)
        value = {
            "task_id": self.goal.task_id if self.goal else "",
            "message": message,
            "failed": failed,
            "active": self.pending is not None and not self.uncertain,
            "execution_unknown": self.uncertain,
            "canceling": self.cancel_requested,
            "retryable": False,
        }
        if failed and self.pending is None:
            value["state"] = 4
        self.emit_task(value)

    def preparation_failed(self, message, code):
        """No ExecuteTask was sent: explicit retry may reuse the same preparation IDs."""
        self.pending = None
        self.events.report_log("[E] [SIM] " + message)
        self.emit_task(
            dict(
                task_id=self.goal.task_id if self.goal else "",
                active=False,
                execution_unknown=False,
                canceling=False,
                failed=True,
                state=4,
                retryable=True,
                error_code=code,
                message=message + "；尚未发送执行 Goal，可人工重试准备",
            )
        )

    def mark_unknown(self, message, code):
        """Keep the original attempt and its futures; silence never proves stopping."""
        if self.uncertain and self.unknown_message == message:
            return
        self.uncertain, self.unknown_message = True, message
        self.events.report_log("[E] [SIM] " + message)
        self.emit_task(
            dict(
                task_id=self.goal.task_id if self.goal else "",
                active=False,
                execution_unknown=True,
                canceling=self.cancel_requested,
                failed=False,
                state=None,
                retryable=False,
                cancel_available=self.pending is not None and not self.cancel_requested,
                error_code=code,
                message=message,
            )
        )

    def execute(self, draft):
        if self.pending is not None or self.uncertain:
            self.events.report_log("[E] [SIM] 原任务仍在处理或结果未知，不接受新执行")
            return
        key = (draft.get("draft_id"), draft.get("revision"))
        if key in self.dispatched:
            self.events.report_log("[E] [SIM] 此版本已下发过执行，不重复提交")
            return
        self.source_draft = {"draft_id": key[0], "draft_revision": key[1]}
        try:
            if draft.get("state") != 1:
                raise ValueError("只能模拟执行明确确认的草稿")
            if key != self.attempt_key or self.prepare_request is None:
                self.goal = None
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
                self.prepare_request, self.attempt_key = request, key
            self.handle = None
            self.cancel_requested = False
            self.cancel_future = self.cancel_deadline = None
            self.last_feedback_seq = 0
            if (
                not self.prepare_client.service_is_ready()
                or not self.executor.server_is_ready()
            ):
                raise ValueError(
                    "模拟执行服务未就绪，请启动 simulator_all 后人工重试准备"
                )
            request = self.prepare_request
            self.pending = (
                "prepare",
                self.prepare_client.call_async(request),
                time.monotonic() + self.RESPONSE_TIMEOUT,
                request.request_id,
            )
            self.emit_task(
                {
                    "task_id": self.goal.task_id,
                    "progress": 0.0,
                    "step": "",
                    "active": True,
                    "failed": False,
                    "execution_unknown": False,
                    "canceling": False,
                    "retryable": False,
                    "state": 0,
                    "error_code": "",
                }
            )
            self.announce("已确认草稿，正在准备模拟执行；不会调用真实 /task/execute")
        except Exception as exc:
            self.preparation_failed(str(exc), "PREPARE_FAILED")

    def on_feedback(self, message):
        feedback = message.feedback
        if (
            self.pending is not None
            and self.goal
            and (feedback.request_id, feedback.task_id, feedback.dispatch_id)
            == (
                self.goal.request_id,
                self.goal.task_id,
                self.goal.dispatch_id,
            )
            and feedback.task.event_seq > self.last_feedback_seq
        ):
            self.last_feedback_seq = feedback.task.event_seq
            phase, future, _, request_id = self.pending
            if phase == "result":
                self.pending = (
                    phase,
                    future,
                    time.monotonic() + self.FEEDBACK_TIMEOUT,
                    request_id,
                )
            if self.uncertain and not self.cancel_requested:
                self.uncertain, self.unknown_message = False, ""
                self.events.report_log("[I] [SIM] 原任务反馈已恢复，继续跟踪原执行")
            self.emit_task(
                {
                    "task_id": feedback.task_id,
                    "message": (
                        self.unknown_message
                        if self.uncertain
                        else feedback.task.message
                        or feedback.message
                        or feedback.task.step
                    ),
                    "step": feedback.task.step,
                    "progress": feedback.task.progress,
                    "active": not self.uncertain,
                    "execution_unknown": self.uncertain,
                    "canceling": self.cancel_requested,
                    "retryable": False,
                    "error_code": "" if not self.uncertain else "EXECUTION_UNKNOWN",
                    "state": None if self.uncertain else feedback.task.state,
                    "failed": False,
                },
            )

    def poll(self):
        if self.pending is None:
            self.poll_cancel(time.monotonic())
            return
        phase, future, deadline, request_id = self.pending
        try:
            if not future.done():
                now = time.monotonic()
                if phase == "result":
                    self.poll_cancel(now)
                    if now >= deadline and not self.uncertain:
                        self.mark_unknown(
                            "执行反馈已失联，结果未知；保留原任务，不自动重发",
                            "FEEDBACK_TIMEOUT",
                        )
                elif now >= deadline and phase == "prepare":
                    self.prepare_client.remove_pending_request(future)
                    future.cancel()
                    self.preparation_failed("模拟准备响应超时", "PREPARE_TIMEOUT")
                elif now >= deadline and phase == "goal":
                    self.cancel_requested = True
                    self.mark_unknown(
                        "执行受理响应超时，结果未知；迟到接受后请求取消，不重发 Goal",
                        "GOAL_RESPONSE_TIMEOUT",
                    )
                return
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
                            "retryable": True,
                            "execution_unknown": False,
                            "canceling": False,
                        }
                    )
                    self.events.report_log("[I] [SIM] " + message)
                    return
                if response.request_id != request_id:
                    raise ValueError("模拟准备响应 ID 不匹配")
                if not response.success:
                    self.preparation_failed(
                        response.message or "模拟准备被拒绝",
                        response.error_code or "PREPARE_REJECTED",
                    )
                    return
                if (
                    response.task.task_id != self.goal.task_id
                    or response.confirmation_context_id != self.goal.input_context_id
                    or response.revision < 1
                ):
                    raise ValueError(response.message or "模拟准备结果不匹配")
                self.goal.stamp = self.node.get_clock().now().to_msg()
                self.goal.confirmed_at = self.goal.stamp
                self.goal.revision = response.revision
                # From this point delivery may have happened, even if the call raises.
                self.dispatched.add(self.attempt_key)
                self.pending = (
                    "goal",
                    self.executor.send_goal_async(
                        self.goal, feedback_callback=self.on_feedback
                    ),
                    time.monotonic() + self.RESPONSE_TIMEOUT,
                    self.goal.request_id,
                )
            elif phase == "goal":
                if not response.accepted:
                    self.pending = None
                    self.uncertain = False
                    self.cancel_requested = False
                    self.announce(
                        "模拟执行 Goal 被拒绝；未开始执行，不自动重发", failed=True
                    )
                    return
                self.handle = response
                self.pending = (
                    "result",
                    response.get_result_async(),
                    time.monotonic() + self.FEEDBACK_TIMEOUT,
                    request_id,
                )
                if self.cancel_requested:
                    self.send_cancel()
            else:
                result = response.result
                if (result.request_id, result.task_id, result.dispatch_id) != (
                    request_id,
                    self.goal.task_id,
                    self.goal.dispatch_id,
                ):
                    raise ValueError("模拟执行结果与当前任务不匹配")
                self.pending, self.handle = None, None
                self.cancel_future = self.cancel_deadline = None
                self.uncertain = False
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
                        "execution_unknown": False,
                        "canceling": False,
                        "retryable": False,
                    },
                )
                self.events.report_log(
                    ("[I] " if result.success else "[E] ") + "[SIM] " + result.message
                )
        except Exception as exc:
            if self.attempt_key not in self.dispatched:
                self.preparation_failed(str(exc), "PREPARE_FAILED")
            else:
                self.pending = None
                if self.handle is not None:
                    self.cancel_requested = True
                self.mark_unknown(f"模拟执行结果未确认：{exc}", "EXECUTION_UNKNOWN")
                if self.handle is not None:
                    self.send_cancel()

    def send_cancel(self):
        if self.handle is None or self.cancel_deadline is not None:
            return
        try:
            self.cancel_deadline = time.monotonic() + self.CANCEL_TIMEOUT
            self.cancel_future = self.handle.cancel_goal_async()
        except Exception as exc:
            self.cancel_deadline = None
            self.mark_unknown(
                f"取消请求发送失败，停止尚未确认：{exc}", "CANCEL_NOT_CONFIRMED"
            )

    def poll_cancel(self, now):
        if self.cancel_deadline is None:
            return
        if self.cancel_future is not None and self.cancel_future.done():
            try:
                response = self.cancel_future.result()
                accepted = any(
                    list(item.goal_id.uuid) == list(self.handle.goal_id.uuid)
                    for item in response.goals_canceling
                )
                if not accepted:
                    raise ValueError("服务端未受理当前 Goal 的取消")
                self.events.report_log("[I] [SIM] 取消已受理，等待原任务返回停止终态")
            except Exception as exc:
                self.cancel_deadline = None
                self.mark_unknown(f"停止尚未确认：{exc}", "CANCEL_NOT_CONFIRMED")
            self.cancel_future = None
        if self.cancel_deadline is not None and now >= self.cancel_deadline:
            self.cancel_deadline = None
            self.mark_unknown(
                "取消后仍未收到停止终态，停止结果未知", "CANCEL_NOT_CONFIRMED"
            )

    @staticmethod
    def cancel_late_goal(future):
        try:
            handle = future.result()
            if handle.accepted:
                handle.cancel_goal_async()
        except Exception:
            pass

    def cancel(self):
        if self.pending is None or self.cancel_requested:
            return
        self.cancel_requested = True
        if self.handle is not None:
            self.send_cancel()
        if self.pending is not None:
            if not self.uncertain:
                self.announce("已请求取消模拟任务，等待服务端确认停止")
            elif self.cancel_future is not None:
                self.announce(self.unknown_message + "；已请求取消，等待停止确认")

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
