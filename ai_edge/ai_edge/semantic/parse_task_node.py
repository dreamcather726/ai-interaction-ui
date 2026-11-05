"""ROS 语义 Action 服务；网络推理在单独线程执行，取消与查询不被阻塞。"""

from ai_edge.common import error_codes as Codes

import time
import json
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
from ai_edge.semantic.dialogue import dialogue_command
from ai_edge.common.catalog import node_catalog
from ai_edge.common.inference_config import LLM_DEFAULTS, validate_llm
from robot_interfaces.action import ParseTask
from robot_interfaces.msg import ComponentStatus, RequestStatus
from robot_interfaces.srv import QueryRobotState


class ParseTaskNode(Node):
    """一个模型同时仅处理一个 Goal；迟到结果不能恢复已取消或超时的任务。"""

    def __init__(self):
        super().__init__("parse_task_node")
        for name, value in LLM_DEFAULTS.items():
            self.declare_parameter(name, value)
        parameters = {name: self.get_parameter(name).value for name in LLM_DEFAULTS}
        validate_llm(parameters)
        self.timeout = self.get_parameter("request_timeout").value
        self.robot_query_timeout = parameters["robot_query_timeout"]
        self.catalog = node_catalog(self)
        self.backend = OllamaBackend(
            self.get_parameter("ollama_url").value,
            self.get_parameter("model").value,
            self.timeout,
            catalog=self.catalog,
            **{
                name: parameters[name]
                for name in ("temperature", "num_ctx", "num_predict", "keep_alive")
            },
        )
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ollama")
        self.pending = None
        self.reserved = False
        self.query_pending = None
        self.robot_query = self.create_client(QueryRobotState, "/ai/query_robot_state")
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
        query = getattr(self, "query_pending", None)
        if query is not None:
            try:
                if query["handle"].is_cancel_requested:
                    raise SemanticError(Codes.CANCELED, "查询已取消")
                if time.monotonic() >= query["deadline"]:
                    raise SemanticError(
                        "ROBOT_QUERY_TIMEOUT",
                        "机器人查询请求超时，当前位置或状态尚未确认。",
                    )
                if query["future"].done():
                    query["response"].set_result(query["future"].result())
                    self.query_pending = None
            except Exception as exc:
                self.robot_query.remove_pending_request(query["future"])
                query["future"].cancel()
                query["response"].set_exception(exc)
                self.query_pending = None
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
        result.stamp = self.get_clock().now().to_msg()
        try:
            self.feedback(handle, ParseTask.Feedback.QUEUED, "语义请求已受理")
            args = list(read_parse_goal(handle.request))
            command = dialogue_command(handle.request.text)
            if command:
                result.outcome, result.intent = result.RESPONDED, "CHAT"
                result.message = result.reply = (
                    "已清空待补充信息，请重新描述完整任务。"
                    if command == "reset"
                    else "已取消当前待补充任务；不会取消已经执行的机器人任务。"
                )
                handle.succeed()
                return result
            response = Future()
            self.pending = {
                "worker": self.worker.submit(self.backend.interact, *args),
                "response": response,
                "handle": handle,
                "deadline": time.monotonic() + self.timeout,
                "discard": False,
            }
            self.feedback(
                handle,
                ParseTask.Feedback.INFERENCING,
                "正在理解输入",
            )
            interaction = await response
            if handle.is_cancel_requested:
                raise SemanticError(Codes.CANCELED, "语义解析已取消")
            self.feedback(
                handle, ParseTask.Feedback.VALIDATING, "意图、实体及任务参数校验已完成"
            )
            result.intent, result.reply = interaction.intent, interaction.reply
            query_results = []
            for kind in interaction.query_types:
                query_results.append(await self.query_robot(handle, kind))
            if handle.is_cancel_requested:
                raise SemanticError(Codes.CANCELED, "交互已取消")
            if query_results:
                result.reply = "\n".join(value["reply"] for value in query_results)
                result.query_result_json = json.dumps(query_results, ensure_ascii=False)
                result.error_code = next(
                    (
                        value["error_code"]
                        for value in query_results
                        if value["error_code"]
                    ),
                    "",
                )
            answer = interaction.task_answer
            if interaction.intent == "UNSUPPORTED":
                result.outcome, result.error_code = (
                    result.UNSUPPORTED,
                    Codes.UNSUPPORTED_TASK,
                )
                result.message = result.reply or "当前尚不支持这项能力。"
                handle.abort()
            elif (
                answer is None
                or interaction.intent == "CLARIFY"
                or any(not item["success"] for item in query_results)
            ):
                result.outcome = result.RESPONDED
                result.message = result.reply or "本轮交互已处理。"
                handle.succeed()
            elif answer.missing_fields:
                assign_candidate(
                    result.candidate, answer.candidate, self.get_clock().now().to_msg()
                )
                result.outcome, result.error_code = (
                    result.NEED_CLARIFICATION,
                    Codes.MISSING_FIELDS,
                )
                result.missing_fields = list(answer.missing_fields)
                result.clarification_question = answer.clarification_question
                result.reply = "\n".join(
                    filter(None, (result.reply, answer.clarification_question))
                )
                result.message = "任务信息不完整，需要补充后再解析"
                handle.succeed()
            else:
                assign_candidate(
                    result.candidate, answer.candidate, self.get_clock().now().to_msg()
                )
                result.outcome, result.success = result.READY, True
                result.reply = "\n".join(
                    filter(
                        None, (result.reply, "任务候选已生成，请在草稿中核对后确认。")
                    )
                )
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

    async def query_robot(self, handle, kind):
        """由模型选择查询类型，程序发送实际ROS请求并采用服务端的事实答复。"""
        value = dict(query_type=kind, success=False, reply="", error_code="", data={})
        if not self.robot_query.service_is_ready():
            value.update(
                reply="机器人查询服务尚未连接，暂时无法确认当前位置或状态。",
                error_code="ROBOT_QUERY_UNAVAILABLE",
            )
            return value
        request = QueryRobotState.Request()
        request.stamp = self.get_clock().now().to_msg()
        request.request_id = handle.request.request_id + ":" + kind
        request.query_type = kind
        response = Future()
        self.query_pending = dict(
            future=self.robot_query.call_async(request),
            response=response,
            handle=handle,
            deadline=time.monotonic() + self.robot_query_timeout,
        )
        try:
            result = await response
            if result.request_id != request.request_id or not result.reply.strip():
                raise SemanticError(
                    "ROBOT_QUERY_INVALID", "机器人查询响应不匹配或缺少答复。"
                )
            value.update(
                success=result.success,
                reply=result.reply,
                error_code=result.error_code,
                data=json.loads(result.data_json or "{}"),
            )
        except SemanticError as exc:
            if exc.code == Codes.CANCELED:
                raise
            value.update(reply=str(exc), error_code=exc.code)
        except Exception:
            value.update(
                reply="机器人查询响应无效，暂时无法确认当前位置或状态。",
                error_code="ROBOT_QUERY_INVALID",
            )
        return value

    def close(self):
        """停止接收新工作；Python 无法强制终止正在进行的 HTTP 线程。"""
        self.timer.cancel()
        self.health_timer.cancel()
        self.server.destroy()
        self.worker.shutdown(wait=False, cancel_futures=True)
        if self.query_pending is not None:
            self.robot_query.remove_pending_request(self.query_pending["future"])
            self.query_pending["future"].cancel()
            if not self.query_pending["response"].done():
                self.query_pending["response"].set_exception(
                    SemanticError(Codes.CANCELED, "节点退出，查询已取消")
                )
        self.robot_query.destroy()


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
