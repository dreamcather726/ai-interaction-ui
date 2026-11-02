"""草稿异步客户端；超时只表示结果未知，不自动重新提交。"""

import time
from uuid import uuid4

from ai_edge.semantic.ros_candidates import assign_candidate
from robot_interfaces.msg import TaskPlanDraft
from robot_interfaces.srv import QueryTaskDraft, ReviewTaskDraft, SaveTaskDraft


class DraftClient:
    def __init__(self, node, events, payload):
        self.node, self.events, self.payload = node, events, payload
        self.clients = {
            "save": node.create_client(SaveTaskDraft, "/ai/task_drafts/save"),
            "review": node.create_client(ReviewTaskDraft, "/ai/task_drafts/review"),
            "query": node.create_client(QueryTaskDraft, "/ai/task_drafts/query"),
        }
        self.subscription = node.create_subscription(
            TaskPlanDraft, "/ai/task_draft", self.on_event, 100
        )
        self.pending = None
        self.initial_query_sent = False

    def ready(self):
        return all(client.service_is_ready() for client in self.clients.values())

    def on_event(self, message):
        self.events.draft.emit(
            dict(operation="event", success=True, draft=self.payload(message))
        )

    def send(self, operation, value):
        if self.pending is not None:
            self.events.draft.emit(dict(success=False, message="草稿操作仍在等待响应"))
            return
        try:
            if operation not in ("save", "query", "confirm", "cancel"):
                raise ValueError("不支持的草稿操作")
            client = self.clients[
                "review" if operation in ("confirm", "cancel") else operation
            ]
            if not client.service_is_ready():
                raise ValueError("草稿服务尚未连接")
            if operation == "save":
                request = SaveTaskDraft.Request()
                for field in (
                    "draft_id",
                    "expected_revision",
                    "session_id",
                    "input_context_id",
                    "source_request_ids",
                ):
                    setattr(request, field, value[field])
                assign_candidate(
                    request.candidate,
                    value["candidate"],
                    self.node.get_clock().now().to_msg(),
                )
            elif operation == "query":
                request = QueryTaskDraft.Request()
                request.draft_id = value.get("draft_id", "")
            else:
                request = ReviewTaskDraft.Request()
                request.operation = (
                    request.CONFIRM if operation == "confirm" else request.CANCEL
                )
                for field in (
                    "draft_id",
                    "revision",
                    "boot_id",
                    "session_id",
                    "input_context_id",
                    "confirmation_token",
                ):
                    setattr(request, field, value[field])
            request.request_id = "draft-request-" + uuid4().hex
            request.stamp = self.node.get_clock().now().to_msg()
            self.pending = (
                operation,
                client,
                client.call_async(request),
                time.monotonic() + 5.0,
                request.request_id,
            )
            self.events.report_log(
                f"草稿请求已发送：{operation}，ID={request.request_id}"
            )
        except (ValueError, KeyError, TypeError) as exc:
            self.events.draft.emit(
                dict(operation=operation, success=False, message=str(exc))
            )

    def poll(self):
        if self.pending is None:
            if not self.ready():
                self.initial_query_sent = False
            elif not self.initial_query_sent:
                self.initial_query_sent = True
                self.send("query", {})
            return
        operation, client, future, deadline, request_id = self.pending
        if not future.done() and time.monotonic() < deadline:
            return
        self.pending = None
        try:
            if not future.done():
                client.remove_pending_request(future)
                future.cancel()
                raise TimeoutError("草稿操作结果未确认，请刷新已保存版本；不会自动重发")
            response = future.result()
            if response.request_id != request_id:
                raise ValueError("草稿响应与请求不一致")
            value = dict(
                operation=operation,
                success=response.success,
                message=response.message,
                error_code=response.error_code,
            )
            if response.success:
                if operation == "query":
                    value["drafts"] = [self.payload(item) for item in response.drafts]
                else:
                    value["draft"] = self.payload(response.draft)
            self.events.draft.emit(value)
        except Exception as exc:
            self.events.draft.emit(
                dict(operation=operation, success=False, unknown=True, message=str(exc))
            )

    def close(self):
        if self.pending is not None:
            _, client, future, _, _ = self.pending
            client.remove_pending_request(future)
            future.cancel()
            self.pending = None
