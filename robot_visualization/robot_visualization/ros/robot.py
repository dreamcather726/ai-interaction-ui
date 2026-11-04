"""One task client for every deployment; only explicit user confirmation dispatches."""

import json
import time
from uuid import uuid4
from copy import deepcopy

from ai_edge.semantic.ros_candidates import assign_candidate
from ai_edge.semantic.semantic_contract import candidate_payload
from rclpy.action import ActionClient
from rclpy.action.graph import get_action_server_names_and_types_by_node
from rclpy.node import NodeNameNonExistentError
from robot_interfaces.msg import NodeHeartbeat, RobotStatus, TaskSnapshot
from robot_interfaces.action import ExecuteTask
from robot_interfaces.srv import CancelTask, PrepareTask, QueryTask

from robot_visualization.task_model import normalize_candidate
from robot_visualization.connection import RobotConnection
from robot_visualization.ros.journal import JournalStore
from robot_visualization.ros.cancel import CancelManager
from robot_visualization.ros.recovery import RecoveryTracker


class RobotClient:
    RESPONSE_TIMEOUT = 5.0
    FEEDBACK_TIMEOUT = 5.0
    CANCEL_TIMEOUT = 5.0

    def __init__(self, node, events):
        self.node, self.events = node, events
        self.config = getattr(node.config, "robot", RobotConnection())
        self.RESPONSE_TIMEOUT = self.config.response_timeout
        self.FEEDBACK_TIMEOUT = self.config.heartbeat_timeout
        self.CANCEL_TIMEOUT = self.config.cancel_timeout
        self.prepare_client = node.create_client(
            PrepareTask, self.config.prepare_service
        )
        self.query_client = node.create_client(QueryTask, self.config.query_service)
        self.cancel_client = node.create_client(CancelTask, self.config.cancel_service)
        self.executor = ActionClient(node, ExecuteTask, self.config.execute_action)
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
        self.prepared = None
        self.prepared_deadline = None
        self.prepared_signature = None
        self.last_status_sequence = 0
        self.execution_boot = None
        self.dispatched = set()
        self.source_draft = {}
        self.boot = None
        self.retired_boots = set()
        self.heartbeat_sequence = 0
        self.heartbeat_seen = None
        self.executor_state = NodeHeartbeat.UNKNOWN
        self.status_seen = None
        self.status_stamp = 0
        self.status_identity = None
        self.query_pending = None
        self.cancel_query_pending = None
        self.last_query_attempt = 0.0
        self.journal_error = ""
        self.deployment_blocked = ""
        self.restored = False
        self.restore_announced = False
        self.terminal_snapshot = None
        self.replay_snapshot = None
        self.preflight_boot = None
        self.tracking_seen = time.monotonic()
        self.journal = JournalStore(self)
        self.cancel_manager = CancelManager(self)
        self.recovery = RecoveryTracker(self)
        self.journal.load()
        from robot_visualization.ros.telemetry import RobotTelemetry

        self.telemetry = RobotTelemetry(
            node,
            events,
            self.config,
            self.on_status,
            self.on_heartbeat,
            self.on_snapshot,
        )

    def provider_counts(self):
        """Graph discovery is a guard, never a command to switch deployment."""
        counts = [0, 0]
        for name, namespace in self.node.get_node_names_and_namespaces():
            try:
                services = self.node.get_service_names_and_types_by_node(
                    name, namespace
                )
                actions = get_action_server_names_and_types_by_node(
                    self.node, name, namespace
                )
            except NodeNameNonExistentError:
                continue  # Discovered nodes may exit between these read calls.
            counts[0] += any(
                endpoint == self.node.resolve_service_name(self.config.prepare_service)
                for endpoint, _ in services
            )
            counts[1] += any(
                endpoint == self.node.resolve_topic_name(self.config.execute_action)
                for endpoint, _ in actions
            )
        return counts

    def assert_single_executor(self, *, require_ready=True):
        if self.deployment_blocked:
            raise ValueError(self.deployment_blocked)
        if any(count > 1 for count in self.provider_counts()):
            raise ValueError("同一任务端点发现多个服务端；请隔离真实与模拟 ROS 图")
        if require_ready and self.journal_error:
            raise ValueError(self.journal_error)
        if not self.heartbeat_fresh():
            raise ValueError("执行端心跳未确认，请等待对应执行节点在线")
        if require_ready and self.executor_state != NodeHeartbeat.RUNNING:
            raise ValueError("执行端尚未完成启动或已降级，不能提交新执行")

    def heartbeat_fresh(self):
        return (
            self.heartbeat_seen is not None
            and time.monotonic() - self.heartbeat_seen < self.FEEDBACK_TIMEOUT
        )

    def on_heartbeat(self, message):
        if (
            message.node_name != self.config.executor_node
            or not message.boot_id
            or message.sequence < 1
        ):
            return
        if message.boot_id in self.retired_boots:
            return
        if self.boot != message.boot_id:
            if self.boot is not None:
                self.retired_boots.add(self.boot)
                self.recovery.stop_query()
                self.status_seen, self.status_identity, self.status_stamp = (
                    None,
                    None,
                    0,
                )
                self.prepared = None
                if self.attempt_key in self.dispatched:
                    self.mark_unknown(
                        "执行端实例已变化，原任务需查询核实，不能重新下发",
                        "EXECUTOR_RESTARTED",
                    )
                elif self.pending is not None or self.prepare_request is not None:
                    if self.pending and self.pending[0] == "prepare":
                        self.prepare_client.remove_pending_request(self.pending[1])
                        self.pending[1].cancel()
                        self.pending = None
                    self.preparation_failed(
                        "执行端已重启，旧预检失效", "EXECUTOR_RESTARTED"
                    )
            self.boot, self.heartbeat_sequence = message.boot_id, 0
        if message.sequence <= self.heartbeat_sequence:
            return
        self.heartbeat_sequence = message.sequence
        self.executor_state = message.state
        # STARTING/DEGRADED still report liveness, not authorization/readiness.
        self.heartbeat_seen = time.monotonic()
        self.events.record.emit(
            "robot_heartbeat",
            dict(
                boot_id=self.boot,
                sequence=message.sequence,
                received_at=self.heartbeat_seen,
            ),
        )
        if (
            self.pending
            and self.pending[0] == "result"
            and self.execution_boot == self.boot
            and not self.uncertain
            and self.status_seen is not None
            and time.monotonic() - self.status_seen < self.config.telemetry_timeout
            and self.status_identity == (self.goal.task_id, self.goal.dispatch_id)
        ):
            self.observe_liveness()

    def on_status(self, message):
        if message.source_node != self.config.executor_node:
            return
        stamp = message.stamp.sec * 10**9 + message.stamp.nanosec
        if stamp <= self.status_stamp:
            return
        self.status_stamp = stamp
        self.status_seen = time.monotonic()
        self.status_identity = (
            (message.active_task_id, message.active_dispatch_id)
            if message.state in (RobotStatus.EXECUTING, RobotStatus.STOPPING)
            else None
        )
        if (
            self.pending
            and self.pending[0] == "result"
            and self.goal
            and self.status_identity != (self.goal.task_id, self.goal.dispatch_id)
        ):
            self.query()
        names = {
            RobotStatus.UNKNOWN: "状态未知",
            RobotStatus.IDLE: "待命",
            RobotStatus.EXECUTING: "执行中",
            RobotStatus.STOPPING: "停止中",
            RobotStatus.ESTOP: "急停",
            RobotStatus.FAULT: "故障",
            RobotStatus.RECOVERING: "恢复中",
        }
        self.events.record.emit(
            "robot_status",
            dict(
                state=message.state,
                mode=names.get(message.state, "未知"),
                message=message.message,
                error_code=message.error_code,
                active_task_id=message.active_task_id,
                active_dispatch_id=message.active_dispatch_id,
                chassis_ready=message.state
                in (RobotStatus.IDLE, RobotStatus.EXECUTING),
                received_at=time.monotonic(),
                stamp_ns=stamp,
            ),
        )

    def on_snapshot(self, snapshot):
        if (
            not self.goal
            or not self.pending
            or self.pending[0] != "result"
            or self.uncertain
            or not self.heartbeat_fresh()
            or self.execution_boot != self.boot
        ):
            return
        if (
            not self.snapshot_matches(snapshot)
            or snapshot.event_seq <= self.last_feedback_seq
        ):
            return
        # A terminal topic snapshot prompts a query; the original Action result
        # or matched QueryTask response is still the authoritative reconciliation.
        if snapshot.state in (3, 4, 5):
            self.query()
            return
        self.last_feedback_seq = snapshot.event_seq
        self.emit_task(
            dict(
                step=snapshot.step,
                progress=snapshot.progress,
                state=snapshot.state,
                message=snapshot.message,
                active=True,
                failed=False,
            )
        )

    def snapshot_matches(self, snapshot):
        try:
            return bool(
                self.goal
                and self.prepare_request
                and snapshot.task.task_id == self.goal.task_id
                and snapshot.revision == self.goal.revision
                and snapshot.active_dispatch_id == self.goal.dispatch_id
                and snapshot.task.source_session_id == self.goal.session_id
                and snapshot.task.source_input_id
                == self.prepare_request.task.source_input_id
                and snapshot.task.source_context_id
                == self.prepare_request.task.source_context_id
                and normalize_candidate(candidate_payload(snapshot.task.candidate))
                == normalize_candidate(
                    candidate_payload(self.prepare_request.task.candidate)
                )
            )
        except (ValueError, TypeError, AttributeError):
            return False

    def query(self):
        return self.recovery.query()

    def emit_task(self, value):
        self.events.record.emit(
            "robot_task",
            dict(
                value,
                dispatch_id=self.goal.dispatch_id if self.goal else "",
                **self.source_draft,
            ),
        )

    def announce(self, message, *, failed=False):
        self.events.report_log(("[E] " if failed else "[I] ") + "[ROBOT] " + message)
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
        self.prepared = None
        self.events.report_log("[E] [ROBOT] " + message)
        self.emit_task(
            dict(
                task_id=self.goal.task_id if self.goal else "",
                active=False,
                execution_unknown=False,
                canceling=False,
                failed=True,
                state=4,
                retryable=True,
                prepared=False,
                preparation_pending=False,
                error_code=code,
                message=message + "；尚未下发执行，可点击“重试准备”",
            )
        )

    def mark_unknown(self, message, code):
        """Keep the original attempt and its futures; silence never proves stopping."""
        if self.uncertain and self.unknown_message == message:
            return
        self.uncertain, self.unknown_message = True, message
        self.events.report_log("[E] [ROBOT] " + message)
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

    @staticmethod
    def draft_signature(draft):
        return json.dumps(
            [
                draft.get(name)
                for name in (
                    "draft_id",
                    "revision",
                    "boot_id",
                    "session_id",
                    "input_context_id",
                    "catalog_revision",
                )
            ]
            + [normalize_candidate(draft["candidate"])],
            sort_keys=True,
            ensure_ascii=False,
        )

    def prepare(self, draft):
        """Preflight only. A matching explicit confirmation must call execute later."""
        if self.pending is not None or self.uncertain or self.restored:
            self.events.report_log("[E] [ROBOT] 原任务仍在处理或结果未知，不接受新执行")
            return
        key = (draft.get("draft_id"), draft.get("revision"))
        if key in self.dispatched:
            self.events.report_log("[E] [ROBOT] 此版本已下发过执行，不重复提交")
            return
        self.source_draft = {
            "draft_id": key[0],
            "draft_revision": key[1],
            "draft_boot_id": draft.get("boot_id"),
            "catalog_revision": draft.get("catalog_revision"),
        }
        try:
            self.recovery.stop_query()  # An older read response cannot overwrite a new draft.
            self.assert_single_executor()
            if draft.get("state") not in (0, 1):
                raise ValueError("只能预检已保存且有效的草稿")
            signature = self.draft_signature(draft)
            if (
                key != self.attempt_key
                or self.prepare_request is None
                or signature != self.prepared_signature
            ):
                self.goal = None
                self.terminal_snapshot = None
                request = PrepareTask.Request()
                request.stamp = self.node.get_clock().now().to_msg()
                request.request_id = "prepare-" + uuid4().hex
                request.confirmation_context_id = "confirm-" + uuid4().hex
                request.task.stamp = request.stamp
                request.task.interface_version = "0.3.1"
                request.task.task_id = "task-" + uuid4().hex
                request.task.source_session_id = draft["session_id"]
                request.task.source_input_id = (
                    f"draft:{draft['draft_id']}:v{draft['revision']}"
                )
                request.task.source_context_id = draft["input_context_id"]
                assign_candidate(
                    request.task.candidate,
                    normalize_candidate(draft["candidate"]),
                    request.stamp,
                )
                self.goal = ExecuteTask.Goal()
                self.goal.request_id = "execute-" + uuid4().hex
                self.goal.task_id = request.task.task_id
                self.goal.dispatch_id = "dispatch-" + uuid4().hex
                self.goal.session_id = draft["session_id"]
                self.goal.input_context_id = request.confirmation_context_id
                self.prepare_request, self.attempt_key = request, key
            self.prepared_signature = signature
            self.prepared = None
            self.handle = None
            self.cancel_requested = False
            self.cancel_future = self.cancel_deadline = None
            self.last_feedback_seq = 0
            self.last_status_sequence = 0
            self.execution_boot = None
            if (
                not self.prepare_client.service_is_ready()
                or not self.executor.server_is_ready()
            ):
                raise ValueError("机器人执行服务未就绪，请启动 执行端 后人工重试准备")
            request = self.prepare_request
            self.preflight_boot = self.boot
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
                    "prepared": False,
                    "preparation_pending": True,
                    "revised_candidate": None,
                }
            )
            self.announce("正在预检草稿；预检不启动动作，待你核对后确认整单执行")
        except Exception as exc:
            self.preparation_failed(str(exc), "PREPARE_FAILED")

    def execute(self, draft):
        if self.pending is not None or self.uncertain or self.restored:
            self.events.report_log(
                "[E] [ROBOT] 原执行仍在处理或结果未知，不能下发新任务"
            )
            return
        if self.attempt_key in self.dispatched:
            self.events.report_log("[E] [ROBOT] 此版本已经下发，不重复执行")
            return
        try:
            self.assert_single_executor()
            if self.boot != self.preflight_boot:
                raise ValueError("预检所属执行端实例已变化，请重新预检")
            if draft.get("state") != 1:
                raise ValueError("草稿尚未通过人工审阅，不能下发执行")
            if (
                self.prepared is None
                or self.prepared_signature != self.draft_signature(draft)
            ):
                raise ValueError("草稿、资料或预检结果已变化，请重新预检并确认")
            response = self.prepared
            now = self.node.get_clock().now()
            expiry = (
                response.confirmation_expires_at.sec * 10**9
                + response.confirmation_expires_at.nanosec
            )
            if now.nanoseconds >= expiry or time.monotonic() >= self.prepared_deadline:
                raise ValueError("预检确认窗口已过期，请重新预检")
            if not self.executor.server_is_ready():
                raise ValueError("机器人执行服务未连接，请重新预检")
            self.goal.stamp = now.to_msg()
            self.goal.confirmed_at = self.goal.stamp
            self.goal.revision = response.revision
            self.execution_boot = self.boot
            self.dispatched.add(self.attempt_key)
            # Persist before transmission; recover ambiguous sends by query.
            try:
                self.journal.save()
            except OSError as exc:
                self.dispatched.remove(self.attempt_key)
                self.preparation_failed(
                    f"本地下发记录写入失败，未发送执行请求：{exc}",
                    "LOCAL_RECORD_WRITE_FAILED",
                )
                return
            self.prepared = None
            self.pending = (
                "goal",
                self.executor.send_goal_async(
                    self.goal, feedback_callback=self.on_feedback
                ),
                time.monotonic() + self.RESPONSE_TIMEOUT,
                self.goal.request_id,
            )
            self.emit_task(
                dict(
                    task_id=self.goal.task_id,
                    active=True,
                    failed=False,
                    prepared=False,
                    preparation_pending=False,
                    retryable=False,
                    execution_unknown=False,
                    canceling=False,
                    state=1,
                    error_code="",
                    message="整份任务已提交执行端，执行端自主推进，不需要逐步确认",
                )
            )
        except Exception as exc:
            if self.attempt_key not in self.dispatched:
                self.preparation_failed(str(exc), "PREPARE_REQUIRED")
            else:
                self.pending = None
                self.mark_unknown(f"执行投递结果未知：{exc}", "GOAL_RESPONSE_UNKNOWN")

    def observe_liveness(self):
        """Heartbeat and progress are independent; neither requires a UI reply."""
        phase, future, _, request_id = self.pending
        self.pending = (
            phase,
            future,
            time.monotonic() + self.FEEDBACK_TIMEOUT,
            request_id,
        )
        if self.uncertain and not self.cancel_requested:
            self.uncertain, self.unknown_message = False, ""
            self.emit_task(
                dict(
                    task_id=self.goal.task_id,
                    active=True,
                    failed=False,
                    execution_unknown=False,
                    state=1,
                    message="执行端在线状态已恢复，继续跟踪原任务",
                )
            )
            self.events.report_log("[I] [ROBOT] 原任务执行端在线状态已恢复")

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
            and self.heartbeat_fresh()
            and self.execution_boot == self.boot
            and self.snapshot_matches(feedback.task)
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
                self.events.report_log("[I] [ROBOT] 原任务反馈已恢复，继续跟踪原执行")
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
        self.telemetry.poll_position()
        self.cancel_manager.poll_cancel_service()
        self.recovery.poll_query()
        if self.replay_snapshot is not None:
            snapshot, self.replay_snapshot = self.replay_snapshot, None
            self.emit_task(
                dict(
                    task_id=self.goal.task_id,
                    state=snapshot.state,
                    progress=snapshot.progress,
                    step=snapshot.step,
                    active=False,
                    failed=snapshot.state in (4, 5),
                    execution_unknown=False,
                    error_code=snapshot.error_code,
                    canceling=False,
                    message="已恢复本地历史终态：" + snapshot.message,
                )
            )
        if self.journal_error and not self.restore_announced:
            self.restore_announced = True
            self.mark_unknown(self.journal_error, "LOCAL_RECORD_INVALID")
        if self.restored and not self.restore_announced:
            self.restore_announced = True
            self.mark_unknown(
                "已恢复原下发记录，正在只读查询执行结果；不会重新下发", "RECOVERING"
            )
        if (
            self.uncertain or self.restored
        ) and time.monotonic() - self.last_query_attempt >= 2.0:
            self.query()
        if (
            self.restored
            and not self.uncertain
            and time.monotonic() - self.tracking_seen >= self.FEEDBACK_TIMEOUT
        ):
            self.mark_unknown("恢复跟踪已失联，保留原任务，不重新下发", "QUERY_TIMEOUT")
        if self.pending is None:
            self.cancel_manager.poll_cancel(time.monotonic())
            if self.prepared is not None:
                expiry = self.prepared.confirmation_expires_at
                if (
                    self.node.get_clock().now().nanoseconds
                    >= expiry.sec * 10**9 + expiry.nanosec
                    or time.monotonic() >= self.prepared_deadline
                ):
                    self.preparation_failed(
                        "预检结果已过期，请重新预检后确认", "CONFIRMATION_EXPIRED"
                    )
            return
        phase, future, deadline, request_id = self.pending
        try:
            if not future.done():
                now = time.monotonic()
                if phase == "result":
                    self.cancel_manager.poll_cancel(now)
                    if now >= deadline and not self.uncertain:
                        self.mark_unknown(
                            "执行反馈已失联，结果未知；保留原任务，不自动重发",
                            "FEEDBACK_TIMEOUT",
                        )
                elif now >= deadline and phase == "prepare":
                    self.prepare_client.remove_pending_request(future)
                    future.cancel()
                    self.preparation_failed("机器人准备响应超时", "PREPARE_TIMEOUT")
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
                    message = "机器人准备已取消，未发送执行 Goal"
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
                    self.events.report_log("[I] [ROBOT] " + message)
                    return
                if response.request_id != request_id:
                    raise ValueError("机器人准备响应 ID 不匹配")
                if not response.success:
                    self.preparation_failed(
                        response.message or "机器人准备被拒绝",
                        response.error_code or "PREPARE_REJECTED",
                    )
                    return
                if (
                    response.task.task_id != self.goal.task_id
                    or response.confirmation_context_id != self.goal.input_context_id
                    or response.revision < 1
                ):
                    raise ValueError(response.message or "机器人准备结果不匹配")
                if any(
                    getattr(response.task, field)
                    != getattr(self.prepare_request.task, field)
                    for field in (
                        "interface_version",
                        "source_session_id",
                        "source_input_id",
                        "source_context_id",
                    )
                ):
                    raise ValueError("预检返回的接口版本或任务来源变化")
                if self.boot != self.preflight_boot or not self.heartbeat_fresh():
                    raise ValueError("预检执行端已失联或重启")
                if normalize_candidate(
                    candidate_payload(response.task.candidate)
                ) != normalize_candidate(
                    candidate_payload(self.prepare_request.task.candidate)
                ):
                    self.preparation_failed(
                        "执行端返回的任务内容变化，请核对建议，手动编辑并保存新版本后预检",
                        "PREPARED_TASK_CHANGED",
                    )
                    self.emit_task(
                        dict(
                            revised_candidate=candidate_payload(response.task.candidate)
                        )
                    )
                    return
                expiry = (
                    response.confirmation_expires_at.sec * 10**9
                    + response.confirmation_expires_at.nanosec
                )
                remaining = (expiry - self.node.get_clock().now().nanoseconds) / 10**9
                if remaining <= 0:
                    raise ValueError("预检结果已经过期，请重新预检")
                self.prepared = deepcopy(response)
                self.prepared_deadline = time.monotonic() + remaining
                self.pending = None
                self.emit_task(
                    dict(
                        task_id=self.goal.task_id,
                        active=False,
                        failed=False,
                        state=0,
                        prepared=True,
                        preparation_pending=False,
                        retryable=False,
                        execution_unknown=False,
                        canceling=False,
                        error_code="",
                        prepared_until=time.monotonic() + remaining,
                        prepared_candidate=candidate_payload(response.task.candidate),
                        message="预检通过；请核对完整草稿后点击“确认并整单执行”",
                    )
                )
            elif phase == "goal":
                if not response.accepted:
                    self.pending = None
                    self.uncertain = False
                    self.cancel_requested = False
                    snapshot = TaskSnapshot()
                    snapshot.task = deepcopy(self.prepare_request.task)
                    snapshot.revision = self.goal.revision
                    snapshot.active_dispatch_id = self.goal.dispatch_id
                    snapshot.stamp = self.node.get_clock().now().to_msg()
                    snapshot.state, snapshot.progress = snapshot.FAILED, 0.0
                    snapshot.error_code = "GOAL_REJECTED"
                    snapshot.message = "执行端明确拒绝此次 Goal，未开始执行"
                    self.terminal_snapshot = snapshot
                    self.journal.save()
                    self.announce(
                        "机器人执行 Goal 被拒绝；未开始执行，不自动重发", failed=True
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
                    self.cancel_manager.send_cancel()
            else:
                result = response.result
                if (result.request_id, result.task_id, result.dispatch_id) != (
                    request_id,
                    self.goal.task_id,
                    self.goal.dispatch_id,
                ):
                    raise ValueError("机器人执行结果与当前任务不匹配")
                if (
                    not self.snapshot_matches(result.task)
                    or result.task.state not in (3, 4, 5)
                    or bool(result.success) != (result.task.state == 3)
                    or response.status != {3: 4, 4: 6, 5: 5}[result.task.state]
                    or self.boot != self.execution_boot
                ):
                    raise ValueError("执行终态、版本或执行实例不匹配，需要查询原任务")
                self.pending, self.handle = None, None
                self.cancel_future = self.cancel_deadline = None
                self.uncertain = False
                self.terminal_snapshot = deepcopy(result.task)
                try:
                    self.journal.save()
                except OSError as exc:
                    self.journal_error = f"终态记录落盘失败，禁止新下发：{exc}"
                    self.events.report_log("[E] " + self.journal_error)
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
                    ("[I] " if result.success else "[E] ") + "[ROBOT] " + result.message
                )
        except Exception as exc:
            if self.attempt_key not in self.dispatched:
                self.preparation_failed(str(exc), "PREPARE_FAILED")
            else:
                self.pending = None
                if self.handle is not None:
                    self.cancel_requested = True
                self.mark_unknown(f"机器人执行结果未确认：{exc}", "EXECUTION_UNKNOWN")
                if self.handle is not None:
                    self.cancel_manager.send_cancel()

    def cancel(self):
        if (
            self.pending is None
            and self.restored
            and not self.uncertain
            and not self.cancel_requested
        ):
            self.cancel_manager.cancel_restored()
            return
        if self.pending is None or self.cancel_requested:
            return
        self.cancel_requested = True
        if self.handle is not None:
            self.cancel_manager.send_cancel()
        if self.pending is not None:
            if not self.uncertain:
                self.announce("已请求取消机器人任务，等待服务端确认停止")
            elif self.cancel_future is not None:
                self.announce(self.unknown_message + "；已请求取消，等待停止确认")

    def close(self):
        # Preserve shutdown policy for this process's original Action only.
        if self.pending is not None:
            self.cancel()
        if self.pending is not None:
            phase, future, _, _ = self.pending
            if phase == "goal":
                future.add_done_callback(self.cancel_manager.cancel_late_goal)
            elif phase == "prepare":
                self.prepare_client.remove_pending_request(future)
                future.cancel()
        self.telemetry.close()
        if self.query_pending is not None:
            self.query_client.remove_pending_request(self.query_pending[0])
            self.query_pending[0].cancel()
        if self.cancel_query_pending is not None:
            self.cancel_client.remove_pending_request(self.cancel_query_pending[0])
            self.cancel_query_pending[0].cancel()
        self.executor.destroy()
