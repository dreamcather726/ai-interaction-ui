"""文字输入也走同一个交互Action；只显示回复，不提交机器人执行。"""

import time
from uuid import uuid4
from types import SimpleNamespace

from robot_interfaces.action import ParseTask
from robot_interfaces.msg import DialogueTurn
from ai_edge.common.catalog import CatalogStore, catalog_ids
from ai_edge.semantic.ros_candidates import assign_candidate
from ai_edge.semantic.semantic_contract import candidate_payload
from ai_edge.semantic.dialogue import dialogue_command
from ai_edge.audio.audio_result_validation import validate_parse_result


class ConversationClient:
    def __init__(self, node, events, payload):
        self.node, self.events, self.payload = node, events, payload
        self.session_id = "text-session-" + uuid4().hex
        self.context_id = "text-context-" + uuid4().hex
        self.active = None
        self.partial = None
        self.partial_context = []
        self.partial_until = 0.0

    def fail(self, request_id, message, code):
        self.events.record.emit(
            "parse",
            dict(
                request_id=request_id,
                success=False,
                outcome=3,
                intent="",
                reply=message,
                message=message,
                error_code=code,
            ),
        )

    def send(self, value):
        request_id, text = value["request_id"], value["text"].strip()
        if self.active is not None:
            self.fail(request_id, "上一条文字请求仍在处理中，请稍后再试。", "LLM_BUSY")
            return
        if not text or len(text) > 4096:
            self.fail(request_id, "请输入1至4096字的内容。", "INVALID_REQUEST")
            return
        if not self.node.parser.server_is_ready():
            self.fail(
                request_id,
                "对话服务尚未连接，请等待语义模型就绪。",
                "DEPENDENCY_UNAVAILABLE",
            )
            return
        try:
            goal = ParseTask.Goal()
            goal.stamp = self.node.get_clock().now().to_msg()
            goal.request_id, goal.session_id = request_id, self.session_id
            goal.input_id, goal.input_context_id = "text:" + request_id, self.context_id
            goal.text = text
            snapshot = CatalogStore(self.node.config.catalog_path).read()
            goal.allowed_station_ids, goal.allowed_object_ids = catalog_ids(snapshot)
            if dialogue_command(text) or time.monotonic() >= self.partial_until:
                self.partial = None
            if self.partial is not None:
                assign_candidate(goal.partial_candidate, self.partial, goal.stamp)
                for entry in self.partial_context:
                    turn = DialogueTurn()
                    turn.stamp = goal.stamp
                    turn.role = turn.USER if entry["role"] == "user" else turn.ASSISTANT
                    turn.text = entry["text"]
                    goal.context.append(turn)
            future = self.node.parser.send_goal_async(goal)
            self.active = dict(
                request_id=request_id,
                text=text,
                goal=goal,
                future=future,
                stage="goal",
                deadline=time.monotonic() + 3.0,
            )
        except Exception as exc:
            self.fail(request_id, f"文字请求未能发送：{exc}", "INVALID_REQUEST")

    @staticmethod
    def cancel_late_goal(future):
        try:
            handle = future.result()
            if handle.accepted:
                handle.cancel_goal_async()
        except Exception:
            pass

    def poll(self):
        job = self.active
        if job is None:
            return
        if time.monotonic() >= job["deadline"]:
            self.close()
            self.fail(
                job["request_id"],
                "文字请求等待超时，结果尚未确认，不会自动重发。",
                "PARSE_TIMEOUT",
            )
            return
        if not job["future"].done():
            return
        try:
            if job["stage"] == "goal":
                handle = job["future"].result()
                if not handle.accepted:
                    raise RuntimeError("语义服务拒绝请求，可能正在处理上一条输入。")
                job.update(
                    handle=handle,
                    future=handle.get_result_async(),
                    stage="result",
                    deadline=time.monotonic() + 300.0,
                )
                return
            wrapped = job["future"].result()
            result = wrapped.result
            goal = job["goal"]
            if any(
                getattr(result, name) != getattr(goal, name)
                for name in ("request_id", "session_id", "input_id", "input_context_id")
            ):
                raise RuntimeError("交互结果与请求来源不匹配")
            stations, objects = catalog_ids(
                CatalogStore(self.node.config.catalog_path).read()
            )
            source = SimpleNamespace(
                request_id=goal.request_id,
                session_id=goal.session_id,
                input_context_id=goal.input_context_id,
            )
            validate_parse_result(
                source,
                wrapped,
                [value for value in stations if value in goal.allowed_station_ids],
                [value for value in objects if value in goal.allowed_object_ids],
                input_id=goal.input_id,
            )
            if result.outcome == result.NEED_CLARIFICATION:
                self.partial = candidate_payload(result.candidate)
                self.partial_context = [
                    dict(role="user", text=job["text"]),
                    dict(role="assistant", text=result.clarification_question),
                ]
                self.partial_until = time.monotonic() + 300.0
            elif result.outcome == result.READY:
                self.partial = None
            self.events.record.emit("parse", self.payload(result))
        except Exception as exc:
            self.fail(job["request_id"], f"对话请求失败：{exc}", "INTERACTION_FAILED")
        self.active = None

    def close(self):
        job, self.active = self.active, None
        if job is not None:
            if job.get("handle") is not None:
                job["handle"].cancel_goal_async()
            else:
                job["future"].add_done_callback(self.cancel_late_goal)
