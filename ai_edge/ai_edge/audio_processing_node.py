"""Accept audio handoffs on the AI side and own the recognition pipeline."""

from pathlib import Path
import wave

import rclpy
from rclpy.node import Node
from robot_interfaces.msg import RequestStatus
from robot_interfaces.srv import ProcessAudio
from ai_edge.audio_pipeline import AudioPipeline
from ai_edge.audio_input import read_audio_handoff


class AudioProcessingNode(Node):
    """Accept a complete file job; ASR and semantic operations stay in ai_edge."""

    def __init__(self):
        super().__init__('audio_processing_node')
        self.declare_parameter('audio_root', 'received_audio')
        self.audio_root = Path(self.get_parameter('audio_root').value).expanduser().resolve()
        self.receipt_pub = self.create_publisher(RequestStatus, '/ai/request_status', 10)
        self.pipeline = AudioPipeline(self, self.receipt_pub.publish)
        self.service = self.create_service(ProcessAudio, '/ai/process_audio', self.process_audio)
        self.get_logger().info(f'AI audio admission ready; shared directory: {self.audio_root}')

    def process_audio(self, request, response):
        receipt = response.receipt
        receipt.stamp = self.get_clock().now().to_msg()
        receipt.request_id = request.request_id
        receipt.source_node = self.get_fully_qualified_name()
        receipt.operation = '/ai/process_audio'
        try:
            path, pcm = read_audio_handoff(request, self.audio_root)
            receipt.success, receipt.error_code, receipt.message = self.pipeline.enqueue(request, path, pcm)
        except FileNotFoundError as exc:
            receipt.error_code, receipt.message = 'FILE_NOT_FOUND', str(exc)
        except (ValueError, OSError, wave.Error, EOFError) as exc:
            receipt.error_code, receipt.message = 'INVALID_AUDIO_HANDOFF', str(exc)
        receipt.stage = RequestStatus.RECEIVED if receipt.success else RequestStatus.REJECTED
        self.receipt_pub.publish(receipt)
        return response


def main(args=None):
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
