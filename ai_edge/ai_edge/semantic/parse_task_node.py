"""ROS 语义 Action 服务；网络推理在单独线程执行，取消与查询不被阻塞。"""

from ai_edge.common import error_codes as Codes

import time
from concurrent.futures import ThreadPoolExecutor

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.task import Future

from ai_edge.semantic.ollama_backend import OllamaBackend
from ai_edge.semantic.semantic_contract import SemanticError
from ai_edge.semantic.semantic_input import read_parse_goal
from ai_edge.semantic.ros_candidates import assign_candidate
from ai_edge.common.catalog import node_catalog
from robot_interfaces.action import ParseTask
from robot_interfaces.msg import ComponentStatus, RequestStatus


class ParseTaskNode(Node):
    """一个模型同时仅处理一个 Goal；迟到结果不能恢复已取消或超时的任务。"""

    def __init__(self):
        super().__init__("parse_task_node")
        for name, value in (
            ("ollama_url", "http://127.0.0.1:11434"),
            ("model", "qwen3.5:2b"),
            ("request_timeout", 290.0),
        ):
            self.declare_parameter(name, value)
        self.timeout = self.get_parameter("request_timeout").value
        self.catalog = node_catalog(self)
        self.backend = OllamaBackend(
            self.get_parameter("ollama_url").value,
            self.get_parameter("model").value,
            self.timeout,
            catalog=self.catalog,
        )
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ollama")
        self.pending = None
        self.reserved = False
        self.receipts = self.create_publisher(RequestStatus, "/ai/request_status", 10)
        self.health_pub = self.create_publisher(
            ComponentStatus, "/robot/component_status", 20
        )
        self.group = ReentrantCallbackGroup()
        self.server = ActionServer(
            self,
            ParseTask,
            "/ai/parse_task",
            self.execute,
            goal_callback=self.accept_goal,
            cancel_callback=lambda handle: CancelResponse.ACCEPT,
            callback_group=self.group,
        )
        self.clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = self.create_timer(0.05, self.poll, clock=self.clock)
        self.health_timer = self.create_timer(
            1.0, self.publish_health, clock=self.clock
        )
        self.get_logger().info(
            f"语义解析 Action 已启动，模型：{self.backend.model}，地址：{self.backend.base_url}"
        )

    def accept_goal(self, goal):
        """拒绝时发布业务回执，因为被拒 Goal 不会产生自定义 Result。"""
        try:
            read_parse_goal(goal)
            if self.reserved or self.pending is not None:
                raise SemanticError(
                    Codes.LLM_BUSY, "上一条语义推理尚未结束，本次未受理"
                )
        except SemanticError as exc:
            receipt = RequestStatus()
            receipt.stamp = self.get_clock().now().to_msg()
            receipt.request_id, receipt.source_node = (
                goal.request_id,
                self.get_fully_qualified_name(),
            )
            receipt.operation, receipt.stage = "/ai/parse_task", RequestStatus.REJECTED
            receipt.error_code, receipt.message = exc.code, str(exc)
            self.receipts.publish(receipt)
            self.get_logger().warning(f"拒绝语义请求：{exc.code}，{exc}")
            return GoalResponse.REJECT
        self.reserved = True
        return GoalResponse.ACCEPT

    def feedback(self, handle, stage, message):
        value = ParseTask.Feedback()
        value.stamp = self.get_clock().now().to_msg()
        value.request_id = handle.request.request_id
        value.stage, value.message = stage, message
        handle.publish_feedback(value)

    def poll(self):
        """取消先于迟到结果；底层 HTTP 未返回时仍保留忙碌槽位。"""
        if self.pending is None:
            return
        job = self.pending
        if not job["response"].done():
            if job["handle"].is_cancel_requested:
                job["discard"] = True
                job["discard_code"] = Codes.CANCELED
                job["response"].set_exception(
                    SemanticError(Codes.CANCELED, "语义解析已取消，迟到结果将丢弃")
                )
            elif time.monotonic() >= job["deadline"]:
                job["discard"] = True
                job["discard_code"] = Codes.PARSE_TIMEOUT
                job["response"].set_exception(
                    SemanticError(Codes.PARSE_TIMEOUT, "语义解析超时，未生成任务候选")
                )
            elif job["worker"].done():
                try:
                    job["response"].set_result(job["worker"].result())
                except Exception as exc:
                    job["response"].set_exception(exc)
        if job["worker"].done():
            if job.get("discard"):
                job["worker"].exception()
                self.get_logger().warning("语义推理收尾结束，迟到结果已丢弃")
            self.pending = None

    def publish_health(self):
        status = ComponentStatus()
        status.stamp = self.get_clock().now().to_msg()
        status.source_node = self.get_fully_qualified_name()
        status.component_id = "parser"
        draining = bool(self.pending and self.pending.get("discard"))
        status.enabled = not draining
        status.state = (
            status.DEGRADED
            if draining
            else status.BUSY if self.pending or self.reserved else status.READY
        )
        status.message = (
            "语义推理超时或取消，正在收尾；迟到结果将丢弃"
            if draining
            else (
                "语义模型正在推理"
                if self.pending or self.reserved
                else "语义服务可受理请求；模型连接状态由实际调用确认"
            )
        )
        status.error_code = self.pending.get("discard_code", "") if draining else ""
        self.health_pub.publish(status)

    async def execute(self, handle):
        """关联原始 ID，映射业务结果与 Action 终态，不触发机器人执行。"""
        result = ParseTask.Result()
        for field in ("request_id", "session_id", "input_id", "input_context_id"):
            setattr(result, field, getattr(handle.request, field))
        result.outcome = result.FAILED
        try:
            self.feedback(handle, ParseTask.Feedback.QUEUED, "语义请求已受理")
            args = read_parse_goal(handle.request)
            response = Future()
            self.pending = {
                "worker": self.worker.submit(self.backend.parse, *args),
                "response": response,
                "handle": handle,
                "deadline": time.monotonic() + self.timeout,
                "discard": False,
            }
            self.feedback(
                handle, ParseTask.Feedback.INFERENCING, "正在使用千问 2B 解析任务"
            )
            answer = await response
            if handle.is_cancel_requested:
                raise SemanticError(Codes.CANCELED, "语义解析已取消")
            self.feedback(
                handle, ParseTask.Feedback.VALIDATING, "模型输出及白名单校验已完成"
            )
            assign_candidate(
                result.candidate, answer.candidate, self.get_clock().now().to_msg()
            )
            if answer.unsupported:
                result.outcome, result.error_code = (
                    result.UNSUPPORTED,
                    Codes.UNSUPPORTED_TASK,
                )
                result.message = (
                    "未识别到支持的导航、抓取、放下或搬运任务，此输入未生成候选"
                )
                handle.abort()
            elif answer.missing_fields:
                result.outcome, result.error_code = (
                    result.NEED_CLARIFICATION,
                    Codes.MISSING_FIELDS,
                )
                result.missing_fields = list(answer.missing_fields)
                result.clarification_question = answer.clarification_question
                result.message = "任务信息不完整，需要补充后再解析"
                handle.succeed()
            else:
                result.outcome, result.success = result.READY, True
                result.message = "任务候选已生成，仍需准备及用户明确确认"
                handle.succeed()
        except Exception as exc:
            result.error_code = getattr(exc, "code", Codes.LLM_UNAVAILABLE)
            result.message = str(exc)
            if handle.is_cancel_requested:
                result.outcome, result.error_code = result.CANCELED, Codes.CANCELED
                handle.canceled()
            else:
                handle.abort()
        finally:
            self.reserved = False
        result.stamp = self.get_clock().now().to_msg()
        self.get_logger().info(
            f"语义解析结束，请求 ID：{result.request_id}，结果：{result.outcome}，"
            f"错误码：{result.error_code}，{result.message}"
        )
        return result

    def close(self):
        """停止接收新工作；Python 无法强制终止正在进行的 HTTP 线程。"""
        self.timer.cancel()
        self.health_timer.cancel()
        self.server.destroy()
        self.worker.shutdown(wait=False, cancel_futures=True)


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = ParseTaskNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.close()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
