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
from robot_interfaces.action import ExecuteTask
from robot_interfaces.msg import TaskSnapshot
from robot_interfaces.srv import PrepareTask
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
        self.world_publisher = self.create_publisher(
            String, "/simulator/world_state", latched_qos()
        )
        self.world_timer = self.create_timer(0.1, self.publish_world)
        self.group = ReentrantCallbackGroup()
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
                if len(self.prepared) >= 100:
                    self.prepared.pop(next(iter(self.prepared)))
                self.prepared[task.task_id] = (
                    deepcopy(task),
                    steps,
                    request.confirmation_context_id,
                    now,
                    now + 300 * 10**9,
                )
            response.success = True
            response.message = (
                "仅模拟：任务已准备，等待本次用户确认上下文的 ExecuteTask"
            )
            response.task = deepcopy(task)
            response.revision = 1
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
            task, _, context, created, expires = prepared
            if (
                request.revision != 1
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
            return GoalResponse.ACCEPT

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
        with self.lock:
            self.sequence += 1
            snapshot.event_seq = self.sequence
            snapshot.stamp = self.get_clock().now().to_msg()
            frame = self.world_frame(snapshot.stamp)
            self.update_snapshot(snapshot, frame)
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
            task, _, _, _, expires = self.reserved
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

    def finish(self, result, snapshot):
        with self.lock:
            self.update_snapshot(snapshot, self.world.frame())
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
