"""In-memory PrepareTask + ExecuteTask demo. No robot commands are published."""

from copy import deepcopy
import json
import sqlite3
import threading
import time
from uuid import uuid4

from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node
from action_msgs.srv import CancelGoal
from robot_interfaces.action import ExecuteTask
from robot_interfaces.msg import (
    DispatchReport,
    MaterialLocation,
    MaterialState,
    NodeHeartbeat,
    RobotStatus,
    TaskSnapshot,
)
from robot_interfaces.srv import CancelTask, PrepareTask, QueryTask
from std_msgs.msg import String

from robot_simulator.common import latched_qos, run_node, set_time, time_ns
from robot_simulator.execution import ExecutionError, SimulationExecution
from robot_simulator.plans import expand_steps, return_step_indices


class ExecuteTaskSimulator(Node):
    def __init__(self):
        super().__init__("execute_task_action_server")
        self.declare_parameter("step_seconds", 2.0)
        self.declare_parameter("linear_speed", 0.8)
        self.declare_parameter("step_timeout", 60.0)
        self.declare_parameter("fail_step", 0)
        self.declare_parameter("catalog_path", "")
        self.declare_parameter("feedback_period", 1.0)
        self.feedback_period = self.get_parameter("feedback_period").value
        if not 0 <= self.feedback_period <= 30:
            raise ValueError("feedback_period 必须在[0,30]内；0只在步骤变化和终态反馈")
        self.declare_parameter(
            "stations_json", '{"station_a":[-2.0,0.0],"station_b":[2.0,0.0]}'
        )
        self.declare_parameter("objects_json", '{"box_1":"station_a"}')
        self.step_seconds = self.get_parameter("step_seconds").value
        if not 0.1 <= self.step_seconds <= 60.0:
            raise ValueError("step_seconds 必须在 [0.1,60] 内")
        self.lock = threading.Lock()
        self.world = SimulationExecution(
            json.loads(self.get_parameter("stations_json").value),
            json.loads(self.get_parameter("objects_json").value),
            speed=self.get_parameter("linear_speed").value,
            action_seconds=self.step_seconds,
            step_timeout=self.get_parameter("step_timeout").value,
            fail_step=self.get_parameter("fail_step").value,
        )
        self.world_boot = uuid4().hex
        self.world_sequence = 0
        self.active_task_id = ""
        self.shutdown_event = threading.Event()
        self.prepared = {}
        self.catalog = None
        self.catalog_revision = None
        self.catalog_initialized = False
        self.catalog_error = ""
        self.station_names = {}
        self.last_catalog_check = 0.0
        catalog_path = self.get_parameter("catalog_path").value
        if catalog_path:
            from ai_edge.common.catalog import CatalogStore

            self.catalog = CatalogStore(catalog_path)
        self.active_dispatch = ""
        self.reserved = None
        self.sequence = 0
        self.revision_counter = 0
        self.task_records = {}
        self.dispatch_records = {}
        self.active_goal = None
        self.started = time.monotonic()
        self.last_public_status = 0.0
        self.world_publisher = self.create_publisher(
            String, "/simulator/world_state", latched_qos()
        )
        self.world_timer = self.create_timer(0.1, self.publish_world)
        self.group = ReentrantCallbackGroup()
        self.status_publisher = self.create_publisher(RobotStatus, "/robot/status", 10)
        self.heartbeat_publisher = self.create_publisher(
            NodeHeartbeat, "/robot/heartbeat", 10
        )
        self.material_publisher = self.create_publisher(
            MaterialState, "/robot/material_state", 10
        )
        self.query_service = self.create_service(
            QueryTask, "/task/query", self.query, callback_group=self.group
        )
        self.cancel_service = self.create_service(
            CancelTask, "/task/cancel", self.cancel_task, callback_group=self.group
        )
        self.cancel_clients = [
            self.create_client(
                CancelGoal, name + "/_action/cancel_goal", callback_group=self.group
            )
            for name in ("/task/execute", "/simulator/task/execute")
        ]
        self.snapshots = self.create_publisher(
            TaskSnapshot, "/task/snapshot", latched_qos()
        )
        self.sim_snapshots = self.create_publisher(
            TaskSnapshot, "/simulator/task/snapshot", latched_qos()
        )
        self.prepare_service = self.create_service(
            PrepareTask, "/task/prepare", self.prepare, callback_group=self.group
        )
        self.sim_prepare_service = self.create_service(
            PrepareTask,
            "/simulator/task/prepare",
            self.prepare,
            callback_group=self.group,
        )
        self.server = ActionServer(
            self,
            ExecuteTask,
            "/task/execute",
            execute_callback=self.execute,
            goal_callback=self.accept,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=self.group,
        )
        self.sim_server = ActionServer(
            self,
            ExecuteTask,
            "/simulator/task/execute",
            execute_callback=self.execute,
            goal_callback=self.accept,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=self.group,
        )
        self.get_logger().info(
            "[SIM] 统一执行调度已就绪：导航到站后推进抓放；位置、物料与任务共用同一状态"
        )
        self.publish_world()

    def prepare(self, request, response):
        now = self.get_clock().now().nanoseconds
        response.stamp = self.get_clock().now().to_msg()
        response.request_id = request.request_id
        task = request.task
        try:
            if task.interface_version != "0.3.1":
                raise ValueError("仅支持 interface_version=0.3.1")
            if any(
                not value.strip() or len(value) > 256
                for value in (
                    request.request_id,
                    request.confirmation_context_id,
                    task.task_id,
                    task.source_session_id,
                    task.source_input_id,
                    task.source_context_id,
                )
            ):
                raise ValueError("任务来源、ID 或确认上下文无效")
            steps = expand_steps(task.candidate)
            with self.lock:
                if any(
                    (
                        record.task.source_session_id,
                        record.task.source_input_id,
                        record.task.source_context_id,
                    )
                    == (
                        task.source_session_id,
                        task.source_input_id,
                        task.source_context_id,
                    )
                    for record in self.task_records.values()
                ):
                    raise ExecutionError(
                        "SOURCE_ALREADY_DISPATCHED",
                        "此草稿来源版本已有下发记录，请查询原任务",
                    )
                if any(
                    report.task_id == task.task_id
                    and report.decision == report.ACCEPTED
                    for report in self.dispatch_records.values()
                ):
                    raise ExecutionError(
                        "TASK_ALREADY_DISPATCHED",
                        "此任务已下发；请查询原任务，不重复准备",
                    )
                if self.active_dispatch:
                    raise ExecutionError(
                        "ROBOT_BUSY", "执行端正在运行整份任务，请等待完成后再预检"
                    )
                self.refresh_catalog(force=True)
                if self.catalog_error:
                    raise ExecutionError(
                        "SCENE_CONFIGURATION_FAILED", self.catalog_error
                    )
                if self.reserved and self.reserved[0].task_id == task.task_id:
                    raise ValueError("同一任务正在模拟执行")
                self.world.validate_plan(steps)
                previous = self.prepared.get(task.task_id)
                if (
                    previous
                    and previous[2] == request.confirmation_context_id
                    and previous[6] == request.request_id
                    and previous[0] == task
                    and now < previous[4]
                ):
                    response.success = True
                    response.task = deepcopy(previous[0])
                    response.revision = previous[5]
                    response.confirmation_context_id = previous[2]
                    set_time(response.confirmation_expires_at, previous[4])
                    response.message = "原预检仍有效，未执行任何动作"
                    return response
                if len(self.prepared) >= 100:
                    self.prepared.pop(next(iter(self.prepared)))
                self.revision_counter += 1
                revision = self.revision_counter
                self.prepared[task.task_id] = (
                    deepcopy(task),
                    steps,
                    request.confirmation_context_id,
                    now,
                    now + 300 * 10**9,
                    self.revision_counter,
                    request.request_id,
                )
            response.success = True
            response.message = (
                "仅模拟：任务已准备，等待本次用户确认上下文的 ExecuteTask"
            )
            response.task = deepcopy(task)
            response.revision = revision
            response.confirmation_context_id = request.confirmation_context_id
            set_time(response.confirmation_expires_at, now + 300 * 10**9)
        except ValueError as exc:
            response.error_code, response.message = getattr(
                exc, "code", "INVALID_REQUEST"
            ), str(exc)
        return response

    def refresh_catalog(self, *, force=False):
        """Caller owns the world lock. Active tasks keep their accepted scene."""
        if self.catalog is None or self.active_dispatch:
            return
        now = time.monotonic()
        if not force and now - self.last_catalog_check < 1.0:
            return
        self.last_catalog_check = now
        try:
            snapshot = self.catalog.read()
            if snapshot["revision"] == self.catalog_revision:
                self.catalog_error = ""
                return
            stations = [row["id"] for row in snapshot["stations"] if row["enabled"]]
            objects = [row["id"] for row in snapshot["objects"] if row["enabled"]]
            self.world.sync_catalog(
                stations, objects, initial=not self.catalog_initialized
            )
            self.station_names = {
                row["id"]: row["name"] for row in snapshot["stations"] if row["enabled"]
            }
            self.catalog_revision = snapshot["revision"]
            self.catalog_initialized = True
            self.catalog_error = ""
            self.prepared.clear()
            self.get_logger().info(
                f"[SIM] 工位／物料已同步，资料版本 {self.catalog_revision}；自动布点仅用于仿真，新物料初始位于 {stations[0]}"
            )
        except (ValueError, sqlite3.Error, OSError) as exc:
            message = str(exc)
            if message != self.catalog_error:
                self.get_logger().warning(f"[SIM] 场景配置未就绪：{message}")
            self.catalog_error = message

    def accept(self, request):
        with self.lock:
            if request.dispatch_id in self.dispatch_records:
                self.get_logger().warning("[SIM] 重复投递ID，保留原记录，不重复执行")
                return GoalResponse.REJECT
            self.refresh_catalog(force=True)
            prepared = self.prepared.get(request.task_id)
            now = self.get_clock().now().nanoseconds
            if self.catalog_error:
                self.get_logger().warning(
                    f"[SIM] 拒绝执行：场景资料尚未同步成功；{self.catalog_error}"
                )
                return GoalResponse.REJECT
            if self.active_dispatch or prepared is None:
                self.get_logger().warning(
                    "[SIM] 拒绝执行：正在执行其他任务，或未 PrepareTask"
                )
                return GoalResponse.REJECT
            task, _, context, created, expires, revision, _ = prepared
            if (
                request.revision != revision
                or not request.request_id.strip()
                or not request.dispatch_id.strip()
                or request.session_id != task.source_session_id
                or request.input_context_id != context
                or not created <= time_ns(request.confirmed_at) <= now <= expires
            ):
                self.get_logger().warning(
                    "[SIM] 拒绝执行：版本、来源或确认有效期不匹配"
                )
                return GoalResponse.REJECT
            try:
                self.world.start(prepared[1], return_step_indices(task.candidate))
            except ExecutionError as exc:
                self.get_logger().warning(f"[SIM] 拒绝执行：{exc.code}；{exc}")
                return GoalResponse.REJECT
            self.reserved = self.prepared.pop(request.task_id)
            self.active_dispatch = request.dispatch_id
            self.active_task_id = request.task_id
            snapshot = TaskSnapshot()
            snapshot.task = deepcopy(task)
            snapshot.revision = revision
            snapshot.active_dispatch_id = request.dispatch_id
            snapshot.stamp = self.get_clock().now().to_msg()
            self.update_snapshot(snapshot, self.world.frame())
            self.task_records[request.task_id] = snapshot
            report = DispatchReport()
            report.stamp = snapshot.stamp
            for field in (
                "request_id",
                "task_id",
                "dispatch_id",
                "revision",
                "session_id",
                "input_context_id",
            ):
                setattr(report, field, getattr(request, field))
            report.decision, report.success, report.message = (
                report.ACCEPTED,
                True,
                "整单已受理",
            )
            self.dispatch_records[request.dispatch_id] = report
            return GoalResponse.ACCEPT

    def query(self, request, response):
        response.stamp = self.get_clock().now().to_msg()
        response.request_id = request.request_id
        with self.lock:
            report = self.dispatch_records.get(request.dispatch_id)
            task_id = request.task_id or (report.task_id if report else "")
            task = self.task_records.get(task_id)
            if task is not None:
                response.task, response.task_found = deepcopy(task), True
                if task_id == self.active_task_id and self.active_dispatch:
                    self.update_snapshot(response.task, self.world.frame())
            if report is not None and (
                not request.task_id or report.task_id == request.task_id
            ):
                response.dispatch, response.dispatch_found = deepcopy(report), True
            response.success = bool(
                (request.task_id or request.dispatch_id)
                and (not request.task_id or response.task_found)
                and (not request.dispatch_id or response.dispatch_found)
            )
        response.message = (
            "原任务查询完成"
            if response.success
            else "未找到原任务／投递；不代表可以重新执行"
        )
        if not response.success:
            response.error_code = "TASK_NOT_FOUND"
        return response

    async def cancel_task(self, request, response):
        response.stamp = self.get_clock().now().to_msg()
        response.request_id, response.task_id = request.request_id, request.task_id
        with self.lock:
            handle = (
                self.active_goal if request.task_id == self.active_task_id else None
            )
            if handle is None or not self.active_dispatch:
                response.error_code, response.message = (
                    "TASK_NOT_ACTIVE",
                    "原任务未在本执行实例运行，请查询原记录",
                )
                return response
            goal_id = deepcopy(handle.goal_id)
        # Use the standard Action cancel transition, never force a private state.
        for client in self.cancel_clients:
            if not client.service_is_ready():
                continue
            cancel = CancelGoal.Request()
            cancel.goal_info.goal_id = goal_id
            result = await client.call_async(cancel)
            if any(
                list(item.goal_id.uuid) == list(goal_id.uuid)
                for item in result.goals_canceling
            ):
                response.success, response.message = True, "取消已受理，需等待停止终态"
                return response
        response.error_code, response.message = (
            "CANCEL_NOT_ACCEPTED",
            "未确认取消受理，请查询原任务",
        )
        return response

    def world_frame(self, stamp):
        frame = self.world.frame()
        self.world_sequence += 1
        frame.update(
            boot_id=self.world_boot,
            event_seq=self.world_sequence,
            stamp={"sec": stamp.sec, "nanosec": stamp.nanosec},
            task_id=self.active_task_id,
            dispatch_id=self.active_dispatch,
            station_names=self.station_names,
            catalog_revision=self.catalog_revision,
            scene_error=self.catalog_error,
            catalog_scene=self.catalog is not None,
        )
        return frame

    def publish_world(self):
        with self.lock:
            self.refresh_catalog()
            frame = self.world_frame(self.get_clock().now().to_msg())
            self.world_publisher.publish(
                String(data=json.dumps(frame, ensure_ascii=False))
            )
            self.publish_public(frame)

    def publish_public(self, frame):
        """The workstation consumes only these typed, public interfaces."""
        stamp = self.get_clock().now().to_msg()
        material = MaterialState()
        material.stamp, material.source_node = stamp, self.get_fully_qualified_name()
        material.boot_id, material.event_seq = self.world_boot, frame["event_seq"]
        material.frame_id, material.known = "map", not bool(self.catalog_error)
        material.held_object_id = frame["held_object_id"]
        for entry in frame["objects"]:
            item = MaterialLocation()
            item.object_id, item.station_id, item.carried = (
                entry["id"],
                entry["station_id"],
                entry["carried"],
            )
            item.position_known = True
            item.position.x, item.position.y = float(entry["x"]), float(entry["y"])
            material.objects.append(item)
        self.material_publisher.publish(material)
        if time.monotonic() - self.last_public_status < 0.5:
            return
        self.last_public_status = time.monotonic()
        status = RobotStatus()
        status.stamp, status.source_node = stamp, self.get_fully_qualified_name()
        status.active_task_id = self.active_task_id if self.active_dispatch else ""
        status.active_dispatch_id = self.active_dispatch
        status.state = (
            status.FAULT
            if self.catalog_error or frame["state"] == "FAILED"
            else status.EXECUTING if self.active_dispatch else status.IDLE
        )
        status.error_code = (
            "SCENE_CONFIGURATION_FAILED" if self.catalog_error else frame["error_code"]
        )
        status.message = self.catalog_error or frame["message"]
        self.status_publisher.publish(status)
        heartbeat = NodeHeartbeat()
        heartbeat.stamp, heartbeat.node_name = stamp, self.get_fully_qualified_name()
        heartbeat.boot_id, heartbeat.sequence = self.world_boot, frame["event_seq"]
        set_time(heartbeat.uptime, (time.monotonic() - self.started) * 10**9)
        set_time(heartbeat.expected_period, 500_000_000)
        heartbeat.state = (
            heartbeat.DEGRADED if self.catalog_error else heartbeat.RUNNING
        )
        self.heartbeat_publisher.publish(heartbeat)

    @staticmethod
    def update_snapshot(snapshot, frame):
        snapshot.state = {
            "IDLE": snapshot.PENDING_CONFIRMATION,
            "RUNNING": snapshot.RUNNING,
            "SUCCEEDED": snapshot.SUCCEEDED,
            "CANCELED": snapshot.CANCELED,
            "FAILED": snapshot.FAILED,
        }[frame["state"]]
        snapshot.step, snapshot.progress = frame["step"], frame["progress"]
        for field in (
            "pickup_completed",
            "dropoff_completed",
            "return_completed",
            "error_code",
        ):
            setattr(snapshot, field, frame[field])

    def publish(self, handle, snapshot, message, state=None):
        snapshot.message = message
        with self.lock:
            self.sequence += 1
            snapshot.event_seq = self.sequence
            snapshot.stamp = self.get_clock().now().to_msg()
            frame = self.world_frame(snapshot.stamp)
            self.update_snapshot(snapshot, frame)
            self.task_records[handle.request.task_id] = deepcopy(snapshot)
            self.world_publisher.publish(
                String(data=json.dumps(frame, ensure_ascii=False))
            )
        snapshot.message = message
        if state is not None:
            snapshot.state = state
        feedback = ExecuteTask.Feedback()
        feedback.stamp = snapshot.stamp
        feedback.request_id = handle.request.request_id
        feedback.task_id = handle.request.task_id
        feedback.dispatch_id = handle.request.dispatch_id
        feedback.task = deepcopy(snapshot)
        feedback.message = snapshot.message
        handle.publish_feedback(feedback)
        self.snapshots.publish(snapshot)
        self.sim_snapshots.publish(snapshot)

    def execute(self, handle):
        request = handle.request
        snapshot = TaskSnapshot()
        with self.lock:
            self.active_goal = handle
            task, _, _, _, expires, _, _ = self.reserved
            snapshot.task = deepcopy(task)
            set_time(snapshot.confirmation_expires_at, expires)
        snapshot.revision = request.revision
        snapshot.active_dispatch_id = request.dispatch_id
        snapshot.progress = 0.0
        snapshot.state = snapshot.RUNNING
        result = ExecuteTask.Result()
        result.request_id, result.task_id, result.dispatch_id = (
            request.request_id,
            request.task_id,
            request.dispatch_id,
        )
        try:
            last_tick = time.monotonic()
            last_feedback = 0.0
            last_step = ""
            while True:
                now = time.monotonic()
                with self.lock:
                    if not self.context.ok() or self.shutdown_event.is_set():
                        self.world.fail(
                            "SIMULATOR_STOPPED", "模拟服务正在关闭，执行已停止"
                        )
                    elif handle.is_cancel_requested:
                        self.world.cancel()
                    else:
                        # Motion cannot jump after a stall; the watchdog still sees wall time.
                        self.world.tick(
                            min(0.1, now - last_tick), wall_dt=now - last_tick
                        )
                    frame = self.world.frame()
                last_tick = now
                if frame["state"] != "RUNNING":
                    break
                if frame["step"] != last_step or (
                    self.feedback_period > 0
                    and now - last_feedback >= self.feedback_period
                ):
                    self.publish(handle, snapshot, "[SIM] " + frame["message"])
                    if frame["step"] != last_step:
                        self.get_logger().info(f"[SIM] 开始动作：{frame['step']}")
                    last_step, last_feedback = frame["step"], now
                self.shutdown_event.wait(0.02)
            result.success = frame["state"] == "SUCCEEDED"
            result.error_code, result.message = frame["error_code"], frame["message"]
            state = {
                "SUCCEEDED": snapshot.SUCCEEDED,
                "CANCELED": snapshot.CANCELED,
                "FAILED": snapshot.FAILED,
            }[frame["state"]]
            if self.context.ok():
                self.publish(handle, snapshot, result.message, state)
                if result.success:
                    handle.succeed()
                elif frame["state"] == "CANCELED":
                    handle.canceled()
                else:
                    handle.abort()
            return self.finish(result, snapshot)
        except Exception as exc:
            with self.lock:
                self.world.fail("SIMULATOR_FAILED", str(exc))
                frame = self.world.frame()
            result.success = frame["state"] == "SUCCEEDED"
            result.error_code, result.message = frame["error_code"], frame["message"]
            if self.context.ok():
                try:
                    self.publish(handle, snapshot, result.message)
                    if handle.is_active:
                        if result.success:
                            handle.succeed()
                        elif frame["state"] == "CANCELED":
                            handle.canceled()
                        else:
                            handle.abort()
                except Exception as reporting_error:
                    self.get_logger().error(
                        f"[SIM] 终态发送失败，世界状态保留：{reporting_error}"
                    )
            return self.finish(result, snapshot)
        finally:
            with self.lock:
                self.active_dispatch, self.reserved = "", None
                self.active_goal = None

    def finish(self, result, snapshot):
        with self.lock:
            self.update_snapshot(snapshot, self.world.frame())
            self.task_records[result.task_id] = deepcopy(snapshot)
        result.stamp = self.get_clock().now().to_msg()
        result.task = deepcopy(snapshot)
        self.get_logger().info(f"[SIM] {result.message}；任务 {result.task_id}")
        return result

    def destroy_node(self):
        self.server.destroy()
        self.sim_server.destroy()
        return super().destroy_node()


def main(args=None):
    run_node(ExecuteTaskSimulator, args, threaded=True)
