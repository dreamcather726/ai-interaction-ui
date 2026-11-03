"""AI 端音频处理调度：依次调用 ASR 和语义解析，不直接实现模型。"""

from ai_edge.common import error_codes as Codes

import time
from ai_edge.common.catalog import catalog_ids, node_catalog
from types import SimpleNamespace

from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rclpy.parameter import Parameter

from ai_edge.audio.audio_job_queue import AudioJobQueue
from ai_edge.audio.audio_result_store import save_asr_text, save_parse_result
from ai_edge.audio.audio_result_validation import (
    ResultValidationError,
    validate_asr_result,
    validate_parse_result,
)
from ai_edge.semantic.dialogue import DialogueStore, dialogue_command
from ai_edge.semantic.ros_candidates import assign_candidate
from ai_edge.semantic.semantic_contract import (
    SemanticError,
    candidate_payload,
    reconcile_candidate,
    validate_candidate,
)
from ai_edge.visualization.audio_visualization import AudioVisualization
from robot_interfaces.action import ParseTask
from robot_interfaces.msg import (
    AudioProcessingState,
    DialogueTurn,
    RecognizedText,
    RequestStatus,
)
from robot_interfaces.srv import RecognizeAudio


class AudioPipeline:
    """串行调度音频任务，使用异步调用避免阻塞其他 ROS 回调。"""

    def __init__(self, node, publish_receipt):
        self.node = node
        self.publish_receipt = publish_receipt

        # 声明并检查节点参数。
        node.declare_parameter("enable_semantic", True)
        node.declare_parameter("asr_timeout", 185.0)
        node.declare_parameter("parse_timeout", 300.0)
        node.declare_parameter("clarification_timeout", 300.0)
        node.declare_parameter("allowed_station_ids", Parameter.Type.STRING_ARRAY)
        node.declare_parameter("allowed_object_ids", Parameter.Type.STRING_ARRAY)
        self.catalog = node_catalog(node)
        for name in ("asr_timeout", "parse_timeout"):
            value = node.get_parameter(name).value
            if not 0 < value <= 300:
                raise ValueError(f"参数 {name} 必须大于 0 且不超过 300 秒")
        self.dialogues = DialogueStore(
            timeout=node.get_parameter("clarification_timeout").value
        )

        # 模型调用只保留在 AI 端；接口端不依赖这些客户端。
        self.asr = node.create_client(RecognizeAudio, "/ai/recognize_audio")
        self.parser = ActionClient(node, ParseTask, "/ai/parse_task")
        self.text_pub = node.create_publisher(RecognizedText, "/ai/asr_text", 10)

        # 本轮队列、展示快照与状态机定时器。
        self.queue = AudioJobQueue()
        self.visualization = AudioVisualization(node)
        self.active = None
        self.clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = node.create_timer(0.05, self.tick, clock=self.clock)

    # 业务回执与音频入队。

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
            self.node.get_logger().info(
                f"音频入队，请求 ID：{msg.request_id}；文件={path}；"
                f"等待队列 {len(self.queue)} 条"
            )
            self.visualization.state(
                {"msg": msg, "path": path.resolve()},
                AudioProcessingState.QUEUED,
                "音频已进入 AI 队列",
            )
        return outcome

    # 阶段状态、异常处理与异步调度。

    def stage(self, name, timeout):
        """记录当前阶段与单调时钟截止时间。"""
        self.active["stage"] = name
        self.active["stage_started"] = self.active["last_progress"] = time.monotonic()
        self.active["deadline"] = self.active["stage_started"] + timeout
        stages = {
            "asr_wait": (AudioProcessingState.WAITING_ASR, "等待 ASR 服务"),
            "asr_result": (AudioProcessingState.RECOGNIZING, "正在识别语音"),
            "parse_wait": (AudioProcessingState.WAITING_PARSE, "等待语义解析服务"),
            "parse_goal": (AudioProcessingState.PARSING, "已提交语义解析请求"),
            "parse_result": (AudioProcessingState.PARSING, "正在进行语义解析"),
        }
        self.node.get_logger().info(
            f"请求 {self.active['msg'].request_id}：{stages[name][1]}；"
            f"本阶段等待上限 {timeout:.1f} 秒"
        )
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
        if code == Codes.NO_SPEECH:
            state = AudioProcessingState.NO_SPEECH
        elif code == Codes.CANCELED:
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
                self.fail(Codes.PIPELINE_FAILED, str(exc))
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
        now = time.monotonic()

        # 超时只结束本次等待，不能把未知结果当作成功或自动重试。
        if now >= job["deadline"]:
            if stage == "asr_result":
                self.asr.remove_pending_request(job["future"])
                job["future"].cancel()
            elif stage == "parse_goal":
                job["future"].add_done_callback(self.cancel_late_goal)
            elif stage == "parse_result":
                job["handle"].cancel_goal_async()
            code = (
                Codes.DEPENDENCY_UNAVAILABLE
                if stage.endswith("wait")
                else (
                    Codes.PARSE_TIMEOUT
                    if stage.startswith("parse")
                    else Codes.ASR_TIMEOUT
                )
            )
            self.fail(code, "AI 处理阶段等待超时；结果尚未确认，不自动重试")
            return

        if now - job["last_progress"] >= 10:
            job["last_progress"] = now
            name = "语义解析" if stage.startswith("parse") else "语音识别"
            self.node.get_logger().info(
                f"请求 {job['msg'].request_id}：仍在等待{name}结果，"
                f"本阶段已等待 {now - job['stage_started']:.0f} 秒，"
                f"距离等待超时还有 {job['deadline'] - now:.0f} 秒"
            )

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
            try:
                goal = self.build_parse_goal(job)
            except SemanticError as exc:
                self.fail(exc.code, str(exc))
                return
            job["future"] = self.parser.send_goal_async(goal)
            self.stage("parse_goal", 3.0)
        elif stage == "parse_goal" and job["future"].done():
            handle = job["future"].result()
            if not handle.accepted:
                self.fail(
                    Codes.PARSE_GOAL_REJECTED,
                    "AI 拒绝语义解析请求，具体原因请查看 AI 端业务回执",
                )
                return
            job["handle"] = handle
            job["future"] = handle.get_result_async()
            self.stage("parse_result", self.node.get_parameter("parse_timeout").value)
        elif stage == "parse_result" and job["future"].done():
            self.accept_parse(job["future"].result())

    # 语义请求组装与对话控制。

    @staticmethod
    def dialogue_key(job):
        msg = job["msg"]
        return msg.session_id, msg.input_context_id

    def build_parse_goal(self, job):
        """将同一上下文的待澄清候选和追问带入下一次模型调用。"""
        goal = ParseTask.Goal()
        msg = job["msg"]
        goal.stamp = self.node.get_clock().now().to_msg()
        goal.request_id, goal.session_id = msg.request_id, msg.session_id
        goal.input_id = "audio:" + msg.segment_id
        goal.input_context_id, goal.text = msg.input_context_id, job["text"]
        goal.allowed_station_ids, goal.allowed_object_ids = self.allowed_ids()
        job["allowed_ids"] = (
            list(goal.allowed_station_ids),
            list(goal.allowed_object_ids),
        )

        captured = msg.capture_started_at.sec * 10**9 + msg.capture_started_at.nanosec
        pending = self.dialogues.get(
            self.dialogue_key(job),
            captured,
            goal.allowed_station_ids,
            goal.allowed_object_ids,
        )
        job["dialogue"] = pending
        if pending is not None:
            assign_candidate(goal.partial_candidate, pending.candidate, goal.stamp)
            turns = []
            for entry in pending.context:
                turn = DialogueTurn()
                turn.stamp = goal.stamp
                turn.role = turn.USER if entry["role"] == "user" else turn.ASSISTANT
                turn.text = entry["text"]
                turns.append(turn)
            goal.context = turns
            self.node.get_logger().info(
                f"请求 {msg.request_id}：继续澄清；会话={msg.session_id}；"
                f"上下文={msg.input_context_id}；待补充={list(pending.missing_fields)}；"
                f"保留候选={pending.candidate}"
            )
        else:
            self.node.get_logger().info(f"请求 {msg.request_id}：开始新的任务理解")
        return goal

    def finish_dialogue_command(self, command):
        """本地清空待补充任务；这些控制词不交给模型生成运动候选。"""
        job = self.active
        result = ParseTask.Result()
        msg = job["msg"]
        result.stamp = self.node.get_clock().now().to_msg()
        result.request_id, result.session_id = msg.request_id, msg.session_id
        result.input_id = "audio:" + msg.segment_id
        result.input_context_id = msg.input_context_id
        result.outcome, result.error_code = result.CANCELED, Codes.CANCELED
        result.message = (
            "已清空待补充信息，请重新描述完整任务"
            if command == "reset"
            else "已取消当前待补充任务"
        )
        job["stage"] = "parse_local"
        self.accept_parse(
            SimpleNamespace(status=GoalStatus.STATUS_CANCELED, result=result)
        )

    # 识别结果和语义结果的接收、校验与发布。

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
        text_path = save_asr_text(job["path"], result.text)
        self.node.get_logger().info(f"识别文字已保存：{text_path}")
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

        command = dialogue_command(result.text)
        if command:
            self.finish_dialogue_command(command)
            return
        self.stage("parse_wait", 3.0)

    def accept_parse(self, wrapped):
        """先独立校验语义结果，再保存实际候选和澄清信息。"""
        job = self.active
        stations, objects = self.allowed_ids()
        try:
            # 同时限制在提交时和当前有效的集合，配置扩张不能扩大已发请求的授权范围。
            old_stations, old_objects = job.get("allowed_ids", (stations, objects))
            stations = [value for value in stations if value in old_stations]
            objects = [value for value in objects if value in old_objects]
            ready = validate_parse_result(job["msg"], wrapped, stations, objects)
        except ResultValidationError as exc:
            self.fail(exc.code, str(exc))
            return

        result = wrapped.result
        try:
            self.update_dialogue(job, result, stations, objects)
        except SemanticError as exc:
            self.fail(exc.code, str(exc))
            return

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

    def allowed_ids(self):
        return catalog_ids(
            self.catalog.read(
                self.node.get_parameter_or("allowed_station_ids").value or [],
                self.node.get_parameter_or("allowed_object_ids").value or [],
            )
        )

    # 跨轮待澄清候选的维护。

    def update_dialogue(self, job, result, stations, objects):
        """只有校验通过的缺参结果才进入会话缓存；完整候选不自动执行。"""
        key = self.dialogue_key(job)
        previous = job.get("dialogue")
        if result.outcome in (result.READY, result.NEED_CLARIFICATION):
            candidate = candidate_payload(result.candidate)
            if previous is not None:
                merged = reconcile_candidate(candidate, previous.candidate)
                if merged != candidate:
                    raise SemanticError(
                        Codes.DIALOGUE_CONTEXT_LOST,
                        "解析结果遗漏了已有信息，请重新回答追问",
                    )
            answer = validate_candidate(candidate, stations, objects)
            if result.outcome == result.NEED_CLARIFICATION:
                self.dialogues.remember(
                    key,
                    job["text"],
                    answer,
                    self.node.get_clock().now().nanoseconds,
                    previous,
                )
                self.node.get_logger().info(
                    f"请求 {result.request_id}：已保存待补充候选；"
                    f"待补充={list(answer.missing_fields)}；"
                    f"追问={answer.clarification_question}；"
                    f"请在 {self.dialogues.timeout:.0f} 秒内再次录音回答"
                )
            else:
                self.dialogues.forget(key)
                self.node.get_logger().info(
                    f"请求 {result.request_id}：候选字段完整且白名单校验通过；"
                    "等待任务准备与用户明确确认，本流程不提交执行"
                )
        elif result.outcome == result.CANCELED:
            self.dialogues.forget(key)
        elif previous is not None:
            result.message += "；上一轮待补充任务仍保留，可继续回答或说“重新开始”"

    # 退出清理。

    def close(self):
        """停止本地调度，并尝试取消尚在进行的语义解析。"""
        self.timer.cancel()
        self.dialogues.clear()
        if not self.node.context.ok():
            self.queue.clear()
            return
        if self.active is not None:
            if self.active.get("handle") is not None:
                self.active["handle"].cancel_goal_async()
            elif self.active.get("stage") == "parse_goal":
                self.active["future"].add_done_callback(self.cancel_late_goal)
        self.queue.clear()
