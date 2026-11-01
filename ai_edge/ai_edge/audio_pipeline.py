"""AI 端音频处理调度：依次调用 ASR 和语义解析，不直接实现模型。"""

import time

from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rclpy.parameter import Parameter

from ai_edge.audio_job_queue import AudioJobQueue
from ai_edge.audio_result_store import save_asr_text, save_parse_result
from ai_edge.audio_result_validation import (
    ResultValidationError,
    validate_asr_result,
    validate_parse_result,
)
from ai_edge.audio_visualization import AudioVisualization
from robot_interfaces.action import ParseTask
from robot_interfaces.msg import AudioProcessingState, RecognizedText, RequestStatus
from robot_interfaces.srv import RecognizeAudio


class AudioPipeline:
    """串行调度音频任务，使用异步调用避免阻塞其他 ROS 回调。"""

    def __init__(self, node, publish_receipt):
        self.node = node
        self.publish_receipt = publish_receipt
        node.declare_parameter("enable_semantic", True)
        node.declare_parameter("asr_timeout", 30.0)
        node.declare_parameter("parse_timeout", 30.0)
        node.declare_parameter("allowed_station_ids", Parameter.Type.STRING_ARRAY)
        node.declare_parameter("allowed_object_ids", Parameter.Type.STRING_ARRAY)
        for name in ("asr_timeout", "parse_timeout"):
            value = node.get_parameter(name).value
            if not 0 < value <= 300:
                raise ValueError(f"参数 {name} 必须大于 0 且不超过 300 秒")
        # 模型调用只保留在 AI 端；接口端不依赖这些客户端。
        self.asr = node.create_client(RecognizeAudio, "/ai/recognize_audio")
        self.parser = ActionClient(node, ParseTask, "/ai/parse_task")
        self.text_pub = node.create_publisher(RecognizedText, "/ai/asr_text", 10)
        self.queue = AudioJobQueue()
        self.visualization = AudioVisualization(node)
        self.active = None
        self.clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = node.create_timer(0.05, self.tick, clock=self.clock)

    def report(self, job, operation, success, message, code=""):
        """统一发布阶段状态；保留原始请求 ID 和稳定错误码。"""
        receipt = RequestStatus()
        receipt.stamp = self.node.get_clock().now().to_msg()
        receipt.request_id = job["msg"].request_id
        receipt.source_node = self.node.get_fully_qualified_name()
        receipt.operation = operation
        receipt.success = success
        receipt.stage = RequestStatus.RECEIVED if success else RequestStatus.REJECTED
        receipt.message, receipt.error_code = message, code
        self.publish_receipt(receipt)
        text = f"处理阶段：{operation}，请求 ID：{receipt.request_id}，错误码：{code}，说明：{message}"
        if success:
            self.node.get_logger().info(text)
        else:
            self.node.get_logger().warning(text)

    def enqueue(self, msg, path, pcm):
        """委托独立队列模块受理任务，调度器不重复实现去重。"""
        before = len(self.queue)
        outcome = self.queue.enqueue(msg, path, pcm, active=self.active is not None)
        if len(self.queue) > before:
            self.visualization.state(
                {"msg": msg, "path": path.resolve()},
                AudioProcessingState.QUEUED,
                "音频已进入 AI 队列",
            )
        return outcome

    def stage(self, name, timeout):
        """记录当前阶段与单调时钟截止时间。"""
        self.active["stage"] = name
        self.active["deadline"] = time.monotonic() + timeout
        stages = {
            "asr_wait": (AudioProcessingState.WAITING_ASR, "等待 ASR 服务"),
            "asr_result": (AudioProcessingState.RECOGNIZING, "正在识别语音"),
            "parse_wait": (AudioProcessingState.WAITING_PARSE, "等待语义解析服务"),
            "parse_goal": (AudioProcessingState.PARSING, "已提交语义解析请求"),
            "parse_result": (AudioProcessingState.PARSING, "正在进行语义解析"),
        }
        self.visualization.state(self.active, *stages[name])

    def fail(self, code, message):
        """报告当前阶段失败，结束本次任务但继续处理后续队列。"""
        job = self.active
        operation = (
            "/ai/parse_task"
            if job["stage"].startswith("parse")
            else "/ai/recognize_audio"
        )
        self.report(job, operation, False, message, code)
        state = AudioProcessingState.FAILED
        if code == "NO_SPEECH":
            state = AudioProcessingState.NO_SPEECH
        elif code == "CANCELED":
            state = AudioProcessingState.CANCELED
        self.visualization.state(job, state, message, code)
        self.active = None

    @staticmethod
    def cancel_late_goal(future):
        """迟到的 Goal 接受响应仍请求取消，避免超时任务继续运行。"""
        try:
            handle = future.result()
            if handle.accepted:
                handle.cancel_goal_async()
        except Exception:
            pass

    def tick(self):
        """驱动状态机，捕获异常以保持节点继续运行。"""
        try:
            self.advance()
        except Exception as exc:
            if self.active is not None:
                if self.active.get("handle") is not None:
                    self.active["handle"].cancel_goal_async()
                self.fail("PIPELINE_FAILED", str(exc))
            else:
                self.node.get_logger().error(
                    f"音频处理流程异常（PIPELINE_FAILED）：{exc}"
                )

    def advance(self):
        """推进当前异步阶段，不在回调内阻塞等待模型。"""
        if self.active is None:
            if not self.queue:
                return
            self.active = self.queue.popleft()
            self.stage("asr_wait", 3.0)
        job = self.active
        stage = job["stage"]
        # 超时只结束本次等待，不能把未知结果当作成功或自动重试。
        if time.monotonic() >= job["deadline"]:
            if stage == "asr_result":
                self.asr.remove_pending_request(job["future"])
                job["future"].cancel()
            elif stage == "parse_goal":
                job["future"].add_done_callback(self.cancel_late_goal)
            elif stage == "parse_result":
                job["handle"].cancel_goal_async()
            code = (
                "DEPENDENCY_UNAVAILABLE"
                if stage.endswith("wait")
                else ("PARSE_TIMEOUT" if stage.startswith("parse") else "ASR_TIMEOUT")
            )
            self.fail(code, "AI 处理阶段等待超时；结果尚未确认，不自动重试")
            return
        # 依次推进：等待 ASR → 识别结果 → 等待语义模型 → Goal → 最终结果。
        if stage == "asr_wait" and self.asr.service_is_ready():
            msg = job["msg"]
            request = RecognizeAudio.Request()
            request.stamp = self.node.get_clock().now().to_msg()
            for field in (
                "request_id",
                "session_id",
                "segment_id",
                "input_context_id",
                "capture_started_at",
                "capture_ended_at",
            ):
                setattr(request, field, getattr(msg, field))
            request.audio_path = str(job["path"])
            job["future"] = self.asr.call_async(request)
            self.stage("asr_result", self.node.get_parameter("asr_timeout").value)
        elif stage == "asr_result" and job["future"].done():
            self.accept_asr(job["future"].result().result)
        elif stage == "parse_wait" and self.parser.server_is_ready():
            goal = ParseTask.Goal()
            msg = job["msg"]
            goal.stamp = self.node.get_clock().now().to_msg()
            goal.request_id = msg.request_id
            goal.session_id = msg.session_id
            goal.input_id = "audio:" + msg.segment_id
            goal.input_context_id = msg.input_context_id
            goal.text = job["text"]
            goal.allowed_station_ids = (
                self.node.get_parameter_or("allowed_station_ids").value or []
            )
            goal.allowed_object_ids = (
                self.node.get_parameter_or("allowed_object_ids").value or []
            )
            job["future"] = self.parser.send_goal_async(goal)
            self.stage("parse_goal", 3.0)
        elif stage == "parse_goal" and job["future"].done():
            handle = job["future"].result()
            if not handle.accepted:
                self.fail(
                    "PARSE_GOAL_REJECTED",
                    "AI 拒绝语义解析请求，具体原因请查看 AI 端业务回执",
                )
                return
            job["handle"] = handle
            job["future"] = handle.get_result_async()
            self.stage("parse_result", self.node.get_parameter("parse_timeout").value)
        elif stage == "parse_result" and job["future"].done():
            self.accept_parse(job["future"].result())

    def accept_asr(self, result):
        """先独立校验 ASR 结果，再保存文字并进入语义阶段。"""
        job = self.active
        try:
            valid = validate_asr_result(job["msg"], result)
        except ResultValidationError as exc:
            self.fail(exc.code, str(exc))
            return
        self.text_pub.publish(result)
        self.visualization.asr_result(result)
        if not valid:
            self.fail(result.error_code, result.message)
            return
        save_asr_text(job["path"], result.text)
        job["text"] = result.text
        self.report(job, "/ai/recognize_audio", True, result.text)
        if not self.node.get_parameter("enable_semantic").value:
            self.visualization.state(
                job,
                AudioProcessingState.ASR_COMPLETED,
                "语音识别完成，本次未启用语义解析",
            )
            self.active = None
            return
        self.stage("parse_wait", 3.0)

    def accept_parse(self, wrapped):
        """先独立校验语义结果，再保存实际候选和澄清信息。"""
        job = self.active
        stations = self.node.get_parameter_or("allowed_station_ids").value or []
        objects = self.node.get_parameter_or("allowed_object_ids").value or []
        try:
            ready = validate_parse_result(job["msg"], wrapped, stations, objects)
        except ResultValidationError as exc:
            self.fail(exc.code, str(exc))
            return
        result = wrapped.result
        path = save_parse_result(job["path"], result)
        self.visualization.parse_result(result)
        state = (
            AudioProcessingState.READY
            if ready
            else {
                ParseTask.Result.NEED_CLARIFICATION: AudioProcessingState.NEED_CLARIFICATION,
                ParseTask.Result.CANCELED: AudioProcessingState.CANCELED,
            }.get(result.outcome, AudioProcessingState.FAILED)
        )
        self.visualization.state(job, state, result.message, result.error_code)
        self.report(
            job,
            "/ai/parse_task",
            ready,
            f"{result.message}；解析结果文件：{path}",
            result.error_code,
        )
        self.active = None

    def close(self):
        """停止本地调度，并尝试取消尚在进行的语义解析。"""
        self.timer.cancel()
        if not self.node.context.ok():
            self.queue.clear()
            return
        if self.active is not None:
            if self.active.get("handle") is not None:
                self.active["handle"].cancel_goal_async()
            elif self.active.get("stage") == "parse_goal":
                self.active["future"].add_done_callback(self.cancel_late_goal)
        self.queue.clear()
