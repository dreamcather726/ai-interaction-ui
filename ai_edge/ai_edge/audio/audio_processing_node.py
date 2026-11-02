"""AI 端接收接口端提交的音频任务，并装配识别处理流程。"""

import wave
from pathlib import Path

import rclpy
from rclpy.node import Node

from ai_edge.audio.audio_input import read_audio_handoff
from ai_edge.audio.audio_pipeline import AudioPipeline
from robot_interfaces.msg import RequestStatus
from robot_interfaces.srv import ProcessAudio


class AudioProcessingNode(Node):
    """受理完整音频文件任务；ASR 和语义相关操作均由 AI 端负责。"""

    def __init__(self):
        super().__init__("audio_processing_node")
        self.declare_parameter("audio_root", "received_audio")
        self.audio_root = (
            Path(self.get_parameter("audio_root").value).expanduser().resolve()
        )
        self.receipt_pub = self.create_publisher(
            RequestStatus, "/ai/request_status", 10
        )
        self.pipeline = AudioPipeline(self, self.receipt_pub.publish)
        self.service = self.create_service(
            ProcessAudio, "/ai/process_audio", self.process_audio
        )
        self.get_logger().info(
            f"AI 音频任务接收服务已就绪；共享目录：{self.audio_root}"
        )

    def process_audio(self, request, response):
        """校验并受理音频任务，立即返回入队回执而非识别结果。"""
        receipt = response.receipt
        receipt.stamp = self.get_clock().now().to_msg()
        receipt.request_id = request.request_id
        receipt.source_node = self.get_fully_qualified_name()
        receipt.operation = "/ai/process_audio"
        try:
            path, pcm = read_audio_handoff(request, self.audio_root)
            receipt.success, receipt.error_code, receipt.message = (
                self.pipeline.enqueue(request, path, pcm)
            )
        except FileNotFoundError as exc:
            receipt.error_code, receipt.message = (
                "FILE_NOT_FOUND",
                f"音频文件不存在：{exc}",
            )
        except (ValueError, OSError, wave.Error, EOFError) as exc:
            receipt.error_code, receipt.message = (
                "INVALID_AUDIO_HANDOFF",
                f"音频交接校验失败：{exc}",
            )
        receipt.stage = (
            RequestStatus.RECEIVED if receipt.success else RequestStatus.REJECTED
        )
        self.receipt_pub.publish(receipt)
        return response


def main(args=None):
    """启动 AI 音频接收入口，并在退出时停止处理流程。"""
    rclpy.init(args=args)
    node = None
    try:
        node = AudioProcessingNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.pipeline.close()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
