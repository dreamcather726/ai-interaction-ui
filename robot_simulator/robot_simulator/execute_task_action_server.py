"""In-memory PrepareTask + ExecuteTask demo. No robot commands are published."""

from copy import deepcopy
import threading
import time

from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node
from robot_interfaces.action import ExecuteTask
from robot_interfaces.msg import TaskSnapshot
from robot_interfaces.srv import PrepareTask

from robot_simulator.common import latched_qos, run_node, set_time, time_ns
from robot_simulator.plans import describe_step, expand_steps


class ExecuteTaskSimulator(Node):
    def __init__(self):
        super().__init__("execute_task_action_server")
        self.declare_parameter("step_seconds", 2.0)
        self.step_seconds = self.get_parameter("step_seconds").value
        if not 0.1 <= self.step_seconds <= 60.0:
            raise ValueError("step_seconds 必须在 [0.1,60] 内")
        self.lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.prepared = {}
        self.active_dispatch = ""
        self.reserved = None
        self.sequence = 0
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
            "[SIM] /task/prepare 与 /task/execute 已就绪；每步只推进模拟时钟"
        )

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
                if self.reserved and self.reserved[0].task_id == task.task_id:
                    raise ValueError("同一任务正在模拟执行")
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
            response.error_code, response.message = "INVALID_REQUEST", str(exc)
        return response

    def accept(self, request):
        with self.lock:
            prepared = self.prepared.get(request.task_id)
            now = self.get_clock().now().nanoseconds
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
            self.reserved = self.prepared.pop(request.task_id)
            self.active_dispatch = request.dispatch_id
            return GoalResponse.ACCEPT

    def publish(self, handle, snapshot, message, state=None):
        with self.lock:
            self.sequence += 1
            snapshot.event_seq = self.sequence
        snapshot.stamp = self.get_clock().now().to_msg()
        snapshot.message = message
        if state is not None:
            snapshot.state = state
        feedback = ExecuteTask.Feedback()
        feedback.stamp = snapshot.stamp
        feedback.request_id = handle.request.request_id
        feedback.task_id = handle.request.task_id
        feedback.dispatch_id = handle.request.dispatch_id
        feedback.task = deepcopy(snapshot)
        handle.publish_feedback(feedback)
        self.snapshots.publish(snapshot)
        self.sim_snapshots.publish(snapshot)

    def execute(self, handle):
        request = handle.request
        snapshot = TaskSnapshot()
        with self.lock:
            task, steps, _, _, expires = self.reserved
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
            for index, step in enumerate(steps, 1):
                snapshot.step = describe_step(index, len(steps), step)
                self.publish(handle, snapshot, "[SIM] 正在模拟：" + snapshot.step)
                deadline = time.monotonic() + self.step_seconds
                last_feedback = time.monotonic()
                while time.monotonic() < deadline:
                    if not self.context.ok() or self.shutdown_event.is_set():
                        raise RuntimeError("模拟服务正在关闭，执行已停止")
                    if handle.is_cancel_requested:
                        snapshot.error_code = "CANCELED"
                        self.publish(
                            handle, snapshot, "模拟执行已取消", snapshot.CANCELED
                        )
                        handle.canceled()
                        result.error_code, result.message = "CANCELED", "模拟执行已取消"
                        return self.finish(result, snapshot)
                    if time.monotonic() - last_feedback >= 2.0:
                        self.publish(
                            handle, snapshot, "[SIM] 正在模拟：" + snapshot.step
                        )
                        last_feedback = time.monotonic()
                    self.shutdown_event.wait(
                        min(0.02, max(0, deadline - time.monotonic()))
                    )
                # Check once more before applying the completed step's flags.
                if handle.is_cancel_requested:
                    snapshot.error_code = "CANCELED"
                    self.publish(handle, snapshot, "模拟执行已取消", snapshot.CANCELED)
                    handle.canceled()
                    result.error_code, result.message = "CANCELED", "模拟执行已取消"
                    return self.finish(result, snapshot)
                if step[0] == "PICK":
                    snapshot.pickup_completed = True
                elif step[0] == "PLACE":
                    snapshot.dropoff_completed = True
                snapshot.progress = min(index / len(steps), 0.999)
            snapshot.progress = 1.0
            self.publish(handle, snapshot, "模拟执行完成", snapshot.SUCCEEDED)
            handle.succeed()
            result.success, result.message = True, "模拟执行完成"
            return self.finish(result, snapshot)
        except Exception as exc:
            result.error_code, result.message = "SIMULATOR_FAILED", str(exc)
            snapshot.error_code = result.error_code
            if self.context.ok():
                self.publish(handle, snapshot, result.message, snapshot.FAILED)
                handle.abort()
            return self.finish(result, snapshot)
        finally:
            with self.lock:
                self.active_dispatch, self.reserved = "", None

    def finish(self, result, snapshot):
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
