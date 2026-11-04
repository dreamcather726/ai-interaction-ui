"""Cancellation coordination using the owning client's single task context."""

import time
from uuid import uuid4

from robot_interfaces.srv import CancelTask


class CancelManager:
    def __init__(self, client):
        self.client = client

    def send_cancel(self):
        client = self.client
        if client.handle is None or client.cancel_deadline is not None:
            return
        try:
            client.cancel_deadline = time.monotonic() + client.CANCEL_TIMEOUT
            client.cancel_future = client.handle.cancel_goal_async()
        except Exception as exc:
            client.cancel_deadline = None
            client.mark_unknown(
                f"取消请求发送失败，停止尚未确认：{exc}", "CANCEL_NOT_CONFIRMED"
            )

    def poll_cancel(self, now):
        client = self.client
        if client.cancel_deadline is None:
            return
        if client.cancel_future is not None and client.cancel_future.done():
            try:
                response = client.cancel_future.result()
                accepted = any(
                    list(item.goal_id.uuid) == list(client.handle.goal_id.uuid)
                    for item in response.goals_canceling
                )
                if not accepted:
                    raise ValueError("服务端未受理当前 Goal 的取消")
                client.events.report_log(
                    "[I] [ROBOT] 取消已受理，等待原任务返回停止终态"
                )
            except Exception as exc:
                client.cancel_deadline = None
                client.mark_unknown(f"停止尚未确认：{exc}", "CANCEL_NOT_CONFIRMED")
            client.cancel_future = None
        if client.cancel_deadline is not None and now >= client.cancel_deadline:
            client.cancel_deadline = None
            client.mark_unknown(
                "取消后仍未收到停止终态，停止结果未知", "CANCEL_NOT_CONFIRMED"
            )

    def poll_cancel_service(self):
        client = self.client
        if not client.cancel_query_pending:
            return
        future, request_id, boot, deadline = client.cancel_query_pending
        if not future.done():
            if time.monotonic() >= deadline:
                client.cancel_client.remove_pending_request(future)
                future.cancel()
                client.cancel_query_pending = None
                client.mark_unknown(
                    "取消响应超时，停止结果未知", "CANCEL_NOT_CONFIRMED"
                )
            return
        client.cancel_query_pending = None
        try:
            response = future.result()
            if (
                boot != client.boot
                or response.request_id != request_id
                or response.task_id != client.goal.task_id
                or not response.success
            ):
                raise ValueError(response.message or "取消未受理")
            client.cancel_deadline = time.monotonic() + client.CANCEL_TIMEOUT
            client.query()
        except Exception as exc:
            client.mark_unknown(f"停止尚未确认：{exc}", "CANCEL_NOT_CONFIRMED")

    def cancel_restored(self):
        client = self.client
        try:
            client.assert_single_executor(require_ready=False)
            if (
                client.execution_boot != client.boot
                or not client.cancel_client.service_is_ready()
            ):
                raise ValueError("原执行实例或取消服务尚未确认")
            request = CancelTask.Request()
            request.stamp = client.node.get_clock().now().to_msg()
            request.request_id = "cancel-" + uuid4().hex
            request.task_id, request.reason = (
                client.goal.task_id,
                "用户明确请求停止原任务",
            )
            client.cancel_query_pending = (
                client.cancel_client.call_async(request),
                request.request_id,
                client.boot,
                time.monotonic() + client.CANCEL_TIMEOUT,
            )
            client.cancel_requested = True
            client.emit_task(
                dict(
                    active=True,
                    canceling=True,
                    message="原任务取消请求已发送，等待停止终态",
                )
            )
        except Exception as exc:
            client.mark_unknown(f"停止尚未确认：{exc}", "CANCEL_NOT_CONFIRMED")

    @staticmethod
    def cancel_late_goal(future):
        try:
            handle = future.result()
            if handle.accepted:
                handle.cancel_goal_async()
        except Exception:
            pass
