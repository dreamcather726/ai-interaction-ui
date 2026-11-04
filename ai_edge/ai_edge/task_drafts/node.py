"""草稿保存、查询与人工审阅服务；不连接机器人执行接口。"""

import sqlite3
import time

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.parameter import Parameter

from ai_edge.common import error_codes as Codes
from ai_edge.semantic.ros_candidates import assign_candidate
from ai_edge.semantic.semantic_contract import candidate_payload
from ai_edge.task_drafts.store import DraftError, DraftStore
from ai_edge.common.catalog import catalog_ids, node_catalog
from robot_interfaces.msg import TaskPlanDraft
from robot_interfaces.srv import QueryTaskDraft, ReviewTaskDraft, SaveTaskDraft


class TaskDraftNode(Node):
    def __init__(self):
        super().__init__("task_draft_node")
        self.declare_parameter("database_path", "task_data/task_drafts.sqlite3")
        self.declare_parameter("confirmation_timeout", 300.0)
        for name in ("allowed_station_ids", "allowed_object_ids"):
            self.declare_parameter(name, Parameter.Type.STRING_ARRAY)
        self.catalog = node_catalog(self)
        self.store = DraftStore(
            self.get_parameter("database_path").value,
            self.get_parameter_or("allowed_station_ids").value or [],
            self.get_parameter_or("allowed_object_ids").value or [],
            self.get_parameter("confirmation_timeout").value,
        )
        self.publisher = self.create_publisher(TaskPlanDraft, "/ai/task_draft", 100)
        self.draft_services = [
            self.create_service(SaveTaskDraft, "/ai/task_drafts/save", self.save),
            self.create_service(ReviewTaskDraft, "/ai/task_drafts/review", self.review),
            self.create_service(QueryTaskDraft, "/ai/task_drafts/query", self.query),
        ]
        self.last_ros = self.get_clock().now().nanoseconds
        self.last_steady = time.monotonic()
        self.steady_clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = self.create_timer(0.5, self.poll, clock=self.steady_clock)
        self.get_logger().info(
            f"任务草稿服务已就绪；数据库={self.get_parameter('database_path').value}；"
            "确认只保存人工审阅结果，尚未连接机器人执行"
        )

    def refresh_catalog(self):
        stations, objects = catalog_ids(
            self.catalog.read(
                self.get_parameter_or("allowed_station_ids").value or [],
                self.get_parameter_or("allowed_object_ids").value or [],
            )
        )
        if (stations, objects) != (self.store.stations, self.store.objects):
            for draft in self.store.invalidate():
                self.publisher.publish(self.message(draft))
            self.store.stations, self.store.objects = stations, objects
            self.get_logger().warning(
                "工位或物料白名单已更新，旧草稿确认失效，请重新保存并核对"
            )

    def message(self, value):
        draft = TaskPlanDraft()
        draft.stamp.sec, draft.stamp.nanosec = divmod(value["updated_ns"], 10**9)
        for field in (
            "boot_id",
            "draft_id",
            "revision",
            "event_seq",
            "state",
            "session_id",
            "input_context_id",
            "source_request_ids",
            "summary",
            "confirmation_token",
        ):
            setattr(draft, field, value[field])
        draft.confirmation_expires_at.sec, draft.confirmation_expires_at.nanosec = (
            divmod(value["expires_ns"], 10**9)
        )
        assign_candidate(draft.candidate, value["candidate"], draft.stamp)
        return draft

    def check(self, request):
        now = self.get_clock().now().nanoseconds
        if not request.request_id.strip() or len(request.request_id) > 256:
            raise DraftError(Codes.INVALID_REQUEST, "草稿请求 ID 无效")
        if request.stamp.sec < 0 or not 0 <= request.stamp.nanosec < 10**9:
            raise DraftError(Codes.INVALID_REQUEST, "请求时间戳格式不合法")
        stamp = request.stamp.sec * 10**9 + request.stamp.nanosec
        if not 0 < stamp <= now + 10**9 or now - stamp > 30 * 10**9:
            raise DraftError(
                Codes.STALE_REQUEST, "请求时间无效或已超过30秒，请刷新后重试"
            )
        return now

    def _handle_draft_request(self, request, response, operation, value):
        """Handle business requests without shadowing rclpy.Node.handle."""
        response.request_id = request.request_id
        try:
            self.refresh_catalog()
            record = self.store.apply(
                request.request_id, operation, value, self.check(request)
            )
            response.draft = self.message(record)
            response.success = True
            response.message = {
                "save": "草稿已保存，请逐步核对后确认",
                "confirm": "草稿已审阅并保存；执行状态以独立任务下发和执行端反馈为准",
                "cancel": "草稿已取消；此操作不代替机器人运动取消接口",
            }[operation]
            self.publisher.publish(response.draft)
            self.get_logger().info(
                f"草稿 {record['draft_id']} v{record['revision']}：{response.message}"
            )
        except (ValueError, sqlite3.Error, OSError) as exc:
            response.error_code = getattr(exc, "code", Codes.DRAFT_STORAGE_FAILED)
            response.message = str(exc)
            self.get_logger().warning(f"草稿操作失败：{response.error_code}，{exc}")
        response.stamp = self.get_clock().now().to_msg()
        return response

    def save(self, request, response):
        value = {
            field: getattr(request, field)
            for field in (
                "draft_id",
                "expected_revision",
                "session_id",
                "input_context_id",
            )
        }
        value["source_request_ids"] = list(request.source_request_ids)
        value["candidate"] = candidate_payload(request.candidate)
        return self._handle_draft_request(request, response, "save", value)

    def review(self, request, response):
        value = {
            field: getattr(request, field)
            for field in (
                "draft_id",
                "revision",
                "boot_id",
                "session_id",
                "input_context_id",
                "confirmation_token",
            )
        }
        operation = {request.CONFIRM: "confirm", request.CANCEL: "cancel"}.get(
            request.operation, "invalid"
        )
        return self._handle_draft_request(request, response, operation, value)

    def query(self, request, response):
        response.request_id = request.request_id
        try:
            self.check(request)
            self.poll()
            response.drafts = [
                self.message(item) for item in self.store.query(request.draft_id)
            ]
            response.success, response.message = True, "已读取草稿记录"
        except (DraftError, sqlite3.Error, OSError) as exc:
            response.error_code = getattr(exc, "code", Codes.DRAFT_STORAGE_FAILED)
            response.message = str(exc)
        response.stamp = self.get_clock().now().to_msg()
        return response

    def poll(self):
        now, steady = self.get_clock().now().nanoseconds, time.monotonic()
        try:
            self.refresh_catalog()
            changed = []
            if (
                abs((now - self.last_ros) - (steady - self.last_steady) * 10**9)
                > 5 * 10**9
            ):
                changed.extend(self.store.invalidate())
                self.get_logger().warning("ROS 时钟发生跳变，旧草稿确认已失效")
            changed.extend(self.store.expire(now))
            for item in changed:
                self.publisher.publish(self.message(item))
        except (sqlite3.Error, ValueError, OSError) as exc:
            self.get_logger().error(f"草稿状态保存失败：{exc}")
        self.last_ros, self.last_steady = now, steady


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = TaskDraftNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.timer.cancel()
            node.store.close()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
