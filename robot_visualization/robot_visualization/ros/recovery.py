"""Read-only recovery queries using the owning client's single task context."""

from copy import deepcopy
import time
from uuid import uuid4

from robot_interfaces.srv import QueryTask


class RecoveryTracker:
    def __init__(self, client):
        self.client = client

    def query(self):
        client = self.client
        if (
            not client.goal
            or client.attempt_key not in client.dispatched
            or client.query_pending is not None
            or not client.query_client.service_is_ready()
            or not client.heartbeat_fresh()
        ):
            return
        request = QueryTask.Request()
        request.stamp = client.node.get_clock().now().to_msg()
        request.request_id = "query-" + uuid4().hex
        request.task_id, request.dispatch_id = (
            client.goal.task_id,
            client.goal.dispatch_id,
        )
        client.query_pending = (
            client.query_client.call_async(request),
            request.request_id,
            client.boot,
            time.monotonic() + client.RESPONSE_TIMEOUT,
        )
        client.last_query_attempt = time.monotonic()

    def stop_query(self):
        client = self.client
        if client.query_pending is not None:
            client.query_client.remove_pending_request(client.query_pending[0])
            client.query_pending[0].cancel()
            client.query_pending = None

    def poll_query(self):
        client = self.client
        if client.query_pending is None:
            return
        future, request_id, boot, deadline = client.query_pending
        if not future.done():
            if time.monotonic() >= deadline:
                client.query_client.remove_pending_request(future)
                future.cancel()
                client.query_pending = None
                client.events.report_log("[W] 原任务查询超时；结果仍未知，不重发任务")
            return
        client.query_pending = None
        try:
            response = future.result()
            if (
                boot != client.boot
                or not client.heartbeat_fresh()
                or response.request_id != request_id
            ):
                raise ValueError("查询来源或请求不匹配")
            report = response.dispatch
            if (
                not response.success
                or not response.task_found
                or not response.dispatch_found
                or (
                    report.task_id,
                    report.dispatch_id,
                    report.revision,
                    report.request_id,
                    report.session_id,
                    report.input_context_id,
                )
                != (
                    client.goal.task_id,
                    client.goal.dispatch_id,
                    client.goal.revision,
                    client.goal.request_id,
                    client.goal.session_id,
                    client.goal.input_context_id,
                )
                or report.decision != report.ACCEPTED
                or not report.success
                or not client.snapshot_matches(response.task)
            ):
                raise ValueError(response.message or "原任务／投递未找到或不匹配")
            snapshot = response.task
            if snapshot.state not in (1, 2, 3, 4, 5):
                raise ValueError("投递记录已受理，但任务状态不是执行或终态")
            terminal = snapshot.state in (3, 4, 5)
            if (
                client.execution_boot == boot
                and snapshot.event_seq < client.last_feedback_seq
            ):
                client.events.report_log("[D] 忽略早于已接收事件的原任务查询快照")
                return
            client.last_feedback_seq = snapshot.event_seq
            client.execution_boot = boot
            client.uncertain = (
                client.uncertain and client.cancel_requested and not terminal
            )
            if terminal:
                client.terminal_snapshot = deepcopy(snapshot)
                client.pending, client.handle = None, None
                client.cancel_future = client.cancel_deadline = None
                client.cancel_requested = False
            client.emit_task(
                dict(
                    task_id=client.goal.task_id,
                    state=snapshot.state,
                    progress=snapshot.progress,
                    step=snapshot.step,
                    message=snapshot.message,
                    active=not terminal and not client.uncertain,
                    failed=snapshot.state in (4, 5),
                    execution_unknown=client.uncertain,
                    canceling=client.cancel_requested or snapshot.state == 2,
                    retryable=False,
                    cancel_available=client.handle is not None
                    or (not terminal and client.cancel_client.service_is_ready()),
                    error_code=snapshot.error_code,
                )
            )
            # A restored Action handle cannot be recreated safely. Follow the
            # same dispatch by read-only queries; never submit a replacement Goal.
            client.restored = not terminal and client.handle is None
            client.tracking_seen = time.monotonic()
            client.journal.save()
        except Exception as exc:
            if client.terminal_snapshot is not None and client.snapshot_matches(
                client.terminal_snapshot
            ):
                client.events.report_log(
                    f"[W] 原任务查询未能核实：{exc}；保留本地已确认终态"
                )
                client.emit_task(
                    dict(
                        message="执行端查询未能核实；保留本地历史终态："
                        + client.terminal_snapshot.message
                    )
                )
            else:
                client.mark_unknown(f"原任务仍未核实：{exc}", "QUERY_UNCONFIRMED")
