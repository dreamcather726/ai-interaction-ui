"""界面 ROS 节点：异步录音请求、状态订阅与按 ID 恢复快照。"""

import os
import time
from uuid import uuid4

from rcl_interfaces.msg import Log
from rclpy.action import ActionClient
from rclpy.node import Node
from rosidl_runtime_py.convert import message_to_ordereddict
from std_srvs.srv import Trigger

from robot_interfaces.action import ParseTask
from robot_interfaces.msg import (
    AudioProcessingState,
    ComponentStatus,
    RecognizedText,
    RequestStatus,
    TaskParseReport,
)
from robot_interfaces.srv import ProcessAudio, QueryAudio, RecognizeAudio
from robot_visualization.config import DashboardConfig
from robot_visualization.state import STAGES


def payload(message):
    value = dict(message_to_ordereddict(message))
    if hasattr(message, "stamp"):
        value["stamp_ns"] = message.stamp.sec * 10**9 + message.stamp.nanosec
    return value


class DashboardNode(Node):
    def __init__(self, events):
        super().__init__(f"voice_dashboard_{os.getpid()}")
        self.events = events
        self.config = DashboardConfig.from_node(self)
        self.start_client = self.create_client(Trigger, "/audio/start_recording")
        self.stop_client = self.create_client(Trigger, "/audio/stop_recording")
        self.query_client = self.create_client(QueryAudio, "/ai/query_audio")
        self.asr_client = self.create_client(RecognizeAudio, "/ai/recognize_audio")
        self.process_client = self.create_client(ProcessAudio, "/ai/process_audio")
        self.parser = ActionClient(self, ParseTask, "/ai/parse_task")
        self.pending = {}
        self.components = {}
        self.sequences = {}
        self.last_availability = 0.0
        self.last_status_log = {}
        self.owned_nodes = set()
        self.started_recording = False
        self.subscriptions_list = [
            self.create_subscription(
                AudioProcessingState, "/ai/audio_state", self.on_state, 10
            ),
            self.create_subscription(
                RecognizedText,
                "/ai/asr_text",
                lambda m: self.events.record.emit("asr", payload(m)),
                10,
            ),
            self.create_subscription(
                TaskParseReport,
                "/ai/parse_result",
                lambda m: self.events.record.emit("parse", payload(m)),
                10,
            ),
            self.create_subscription(
                ComponentStatus, "/robot/component_status", self.on_component, 20
            ),
            self.create_subscription(
                RequestStatus, "/ai/request_status", self.on_receipt, 10
            ),
            self.create_subscription(Log, "/rosout", self.on_log, 100),
        ]

    def on_component(self, message):
        if message.component_id in ("asr", "microphone"):
            self.components[message.component_id] = (message, time.monotonic())

    def on_receipt(self, message):
        tag = "成功" if message.success else message.error_code or "失败"
        self.events.report_log(
            f"[{message.operation}] {tag} · {message.message} · {message.request_id}"
        )

    def on_log(self, message):
        names = {
            "asr_node",
            "parse_task_node",
            "audio_processing_node",
            "audio_receiver_node",
            "microphone_sender_node",
        }
        if message.name in names and message.name not in self.owned_nodes:
            self.events.report_log(f"[{message.name}] {message.msg}")

    def on_state(self, message):
        key = (message.source_node, message.boot_id, message.request_id)
        previous = self.sequences.get(key, 0)
        if message.event_seq <= previous:
            return
        self.sequences[key] = message.event_seq
        if len(self.sequences) > 1024:
            self.sequences.pop(next(iter(self.sequences)))
        self.events.record.emit("state", payload(message))
        stage_name = STAGES.get(message.stage, (str(message.stage), "idle"))[0]
        self.events.report_log(
            f"音频处理 [{message.request_id}]：{stage_name}；{message.message}"
            + (f"；错误码={message.error_code}" if message.error_code else "")
        )
        if previous == 0 or message.event_seq > previous + 1 or message.stage >= 5:
            self.request_query(message.request_id)

    def request_control(self, operation):
        if "control" in self.pending:
            return
        client = self.start_client if operation == "start" else self.stop_client
        if not client.service_is_ready():
            self.events.report_log(f"录音请求 {operation} 未发送：设备服务尚未发现")
            self.events.control.emit(
                {"success": False, "message": "录音服务尚未发现，请等待设备连接"}
            )
            return
        future = client.call_async(Trigger.Request())
        self.events.report_log(
            f"已发送录音请求：{operation}，等待设备响应（最多 3 秒）"
        )
        self.pending["control"] = (client, future, time.monotonic() + 3.0, operation)

    def request_query(self, request_id):
        key = "query:" + request_id
        if key in self.pending or not self.query_client.service_is_ready():
            return
        if len(self.pending) >= 20:
            self.events.report_log("查询请求过多，请稍后再试")
            return
        request = QueryAudio.Request()
        request.stamp = self.get_clock().now().to_msg()
        request.request_id = "ui-query-" + uuid4().hex
        request.target_request_id = request_id
        self.pending[key] = (
            self.query_client,
            self.query_client.call_async(request),
            time.monotonic() + 3.0,
            "query",
        )

    def poll(self):
        now = time.monotonic()
        for key, (client, future, deadline, operation) in list(self.pending.items()):
            if future.done():
                del self.pending[key]
                try:
                    response = future.result()
                    if operation == "query":
                        self.accept_snapshot(response)
                    else:
                        if response.success:
                            self.started_recording = operation == "start"
                        self.events.report_log(
                            f"录音请求 {operation} 返回："
                            f"{'成功' if response.success else '失败'}；{response.message}"
                        )
                        self.events.control.emit(
                            {"success": response.success, "message": response.message}
                        )
                except Exception as exc:
                    self.report_error(operation, f"请求失败：{exc}")
            elif now >= deadline:
                del self.pending[key]
                client.remove_pending_request(future)
                future.cancel()
                self.report_error(operation, "请求响应超时，结果未确认；不会自动重发")
        if now - self.last_availability >= 0.4:
            self.last_availability = now
            self.publish_availability(now)

    def report_error(self, operation, message):
        self.events.report_log(f"操作 {operation} 失败：{message}")
        if operation != "query":
            self.events.control.emit({"success": False, "message": message})

    def accept_snapshot(self, response):
        if not response.success or not response.found:
            self.events.report_log(
                f"快照查询：{response.error_code} {response.message}"
            )
            return
        self.events.record.emit("state", payload(response.state))
        if response.asr_available:
            self.events.record.emit("asr", payload(response.asr))
        if response.parse_available:
            self.events.record.emit("parse", payload(response.parse))

    def publish_availability(self, now):
        fresh = {
            name: msg
            for name, (msg, seen) in self.components.items()
            if now - seen < 3.0
        }
        microphone, asr = fresh.get("microphone"), fresh.get("asr")
        recording = microphone.state == microphone.BUSY if microphone else None
        if recording is False:
            self.started_recording = False
        ready = bool(asr and asr.state == asr.READY and asr.enabled)
        start, stop = (
            self.start_client.service_is_ready(),
            self.stop_client.service_is_ready(),
        )
        states = {
            1: ("加载中", "active"),
            2: ("已就绪", "good"),
            3: ("处理中", "active"),
            4: ("状态异常", "warn"),
            5: ("故障", "bad"),
        }

        def available(value):
            return ("服务在线", "good") if value else ("未连接", "idle")

        services = {
            "microphone": (
                states.get(microphone.state, ("未确认", "idle"))
                if microphone
                else available(start and stop)
            ),
            "asr": (
                states.get(asr.state, ("未确认", "idle"))
                if asr
                else ("等待模型状态", "idle")
            ),
            "parser": available(self.parser.server_is_ready()),
            "processor": available(self.process_client.service_is_ready()),
            "receiver": available("audio_receiver_node" in self.get_node_names()),
        }
        names = {
            "microphone": "麦克风",
            "asr": "语音识别",
            "parser": "语义解析",
            "processor": "AI 编排",
            "receiver": "音频接收",
        }
        for name, status in services.items():
            self.log_status_change(
                name, status, f"服务状态：{names[name]} → {status[0]}"
            )
        blockers = []
        if not start:
            blockers.append("录音开始服务未连接")
        if recording is None:
            blockers.append("麦克风录音状态未确认")
        elif recording:
            blockers.append("麦克风正在录音")
        if not ready:
            blockers.append("ASR 模型未就绪")
        reason = "；".join(blockers)
        self.log_status_change(
            "recording_gate",
            reason,
            (
                f"开始录音暂不可用：{reason}"
                if reason
                else "设备与 ASR 已就绪，可以开始录音"
            ),
        )
        for name, component in (("asr", asr), ("microphone", microphone)):
            if component and component.error_code:
                detail = (component.error_code, component.message)
                self.log_status_change(
                    name + "_error",
                    detail,
                    f"{names[name]}异常：{component.error_code}；{component.message}",
                )
            else:
                self.last_status_log.pop(name + "_error", None)
        self.events.availability.emit(
            {
                "services": services,
                "start": start,
                "stop": stop,
                "recording": recording,
                "asr_ready": ready,
                "query": self.query_client.service_is_ready(),
                "connected": self.process_client.service_is_ready(),
                "hint": (
                    microphone.message
                    if microphone and microphone.error_code
                    else (
                        asr.message if asr and not ready else "等待设备与模型的实时状态"
                    )
                ),
            }
        )

    def log_status_change(self, key, value, message):
        """只打印实际变化；0.4 秒轮询和心跳不重复输出相同提示。"""
        if key not in self.last_status_log or self.last_status_log[key] != value:
            self.last_status_log[key] = value
            self.events.report_log(message)

    def close(self):
        for client, future, _, _ in self.pending.values():
            client.remove_pending_request(future)
            future.cancel()
        self.pending.clear()
        self.parser.destroy()
