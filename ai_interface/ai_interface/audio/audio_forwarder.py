"""将已保存的音频及来源信息打包转交 AI 端，不处理识别内容。"""

import time
from collections import deque

from rclpy.clock import Clock, ClockType

from robot_interfaces.msg import RequestStatus
from robot_interfaces.srv import ProcessAudio


class AudioForwarder:
    """仅提交音频任务，并报告 AI 端是否受理。"""

    def __init__(self, node):
        self.node = node
        node.declare_parameter("auto_forward", True)
        self.client = node.create_client(ProcessAudio, "/ai/process_audio")
        self.queue = deque()
        self.active = None
        self.clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = node.create_timer(0.05, self.tick, clock=self.clock)

    def report(self, request_id, code, message):
        """发布音频交接失败回执，不将交接失败当作识别失败。"""
        result = RequestStatus()
        result.stamp = self.node.get_clock().now().to_msg()
        result.request_id = request_id
        result.source_node = self.node.get_fully_qualified_name()
        result.operation = "/ai/process_audio"
        result.stage = RequestStatus.REJECTED
        result.message, result.error_code = message, code
        self.node.receipt_pub.publish(result)
        self.node.get_logger().warning(f"音频转交异常（{code}）：{message}")

    def enqueue(self, msg, path):
        """将来源元数据和确定的文件路径组成请求并加入发送队列。"""
        if not self.node.get_parameter("auto_forward").value:
            return
        if len(self.queue) + (self.active is not None) >= 10:
            self.report(
                msg.request_id, "FORWARD_QUEUE_FULL", "音频已保存，但转交队列已满"
            )
            return
        # 只交付完整来源信息和已经关闭写入的 WAV 路径，不执行识别。
        request = ProcessAudio.Request()
        for field in (
            "stamp",
            "request_id",
            "session_id",
            "segment_id",
            "input_context_id",
            "capture_started_at",
            "capture_ended_at",
            "sample_rate",
            "channels",
            "encoding",
        ):
            setattr(request, field, getattr(msg, field))
        request.audio_path = str(path.resolve())
        self.queue.append(request)

    def tick(self):
        """轮询服务可用性和异步响应，统一处理超时与回执校验。"""
        if self.active is None:
            if not self.queue:
                return
            self.active = {
                "request": self.queue.popleft(),
                "deadline": time.monotonic() + 3,
            }
        job = self.active
        request = job["request"]
        try:
            if time.monotonic() >= job["deadline"]:
                code = "DEPENDENCY_UNAVAILABLE"
                if "future" in job:
                    self.client.remove_pending_request(job["future"])
                    job["future"].cancel()
                    code = "FORWARD_TIMEOUT"
                self.report(
                    request.request_id,
                    code,
                    "AI 端受理状态未确认；已保留文件，不自动重试",
                )
                self.active = None
            elif "future" not in job:
                if self.client.service_is_ready():
                    job["future"] = self.client.call_async(request)
                    job["deadline"] = time.monotonic() + 3
            elif job["future"].done():
                receipt = job["future"].result().receipt
                if (
                    receipt.request_id != request.request_id
                    or receipt.operation != "/ai/process_audio"
                    or not receipt.message.strip()
                    or receipt.stage
                    != (
                        RequestStatus.RECEIVED
                        if receipt.success
                        else RequestStatus.REJECTED
                    )
                    or (receipt.success and receipt.error_code)
                    or (not receipt.success and not receipt.error_code)
                ):
                    self.report(
                        request.request_id,
                        "INVALID_AI_RECEIPT",
                        "AI 端受理回执字段不符合约定",
                    )
                else:
                    self.node.get_logger().info(
                        f"AI 任务交接：请求 ID={request.request_id}，说明：{receipt.message}"
                    )
                # AI 服务自行发布受理回执，接口端不重复发布。
                self.active = None
        except Exception as exc:
            self.report(request.request_id, "FORWARD_FAILED", str(exc))
            self.active = None

    def close(self):
        """停止转交定时器并清理尚未完成的本地请求。"""
        self.timer.cancel()
        self.queue.clear()
        if self.active is not None and "future" in self.active:
            self.client.remove_pending_request(self.active["future"])
            self.active["future"].cancel()
        self.active = None
