"""Accept audio handoffs on the AI side and own the recognition pipeline."""

from pathlib import Path
import wave

import rclpy
from rclpy.node import Node
from robot_interfaces.msg import RequestStatus
from robot_interfaces.srv import ProcessAudio
from ai_edge.audio_pipeline import AudioPipeline


class AudioProcessingNode(Node):
    """Accept a complete file job; ASR and semantic operations stay in ai_edge."""

    def __init__(self):
        super().__init__('audio_processing_node')
        self.declare_parameter('audio_root', 'received_audio')
        self.audio_root = Path(self.get_parameter('audio_root').value).expanduser().resolve()
        self.receipt_pub = self.create_publisher(RequestStatus, '/ai/request_status', 10)
        self.pipeline = AudioPipeline(self)
        self.service = self.create_service(ProcessAudio, '/ai/process_audio', self.process_audio)
        self.get_logger().info(f'AI audio admission ready; shared directory: {self.audio_root}')

    def process_audio(self, request, response):
        receipt = response.receipt
        receipt.stamp = self.get_clock().now().to_msg()
        receipt.request_id = request.request_id
        receipt.source_node = self.get_fully_qualified_name()
        receipt.operation = '/ai/process_audio'
        try:
            if not all(value.strip() for value in (
                    request.request_id, request.session_id, request.segment_id, request.input_context_id)):
                raise ValueError('Source IDs must be nonempty')
            path = Path(request.audio_path)
            if not path.is_absolute():
                raise ValueError('audio_path must be absolute')
            path = path.resolve(strict=True)
            if not path.is_relative_to(self.audio_root):
                raise ValueError('Audio file is outside configured audio_root')
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('WAV file exceeds 2 MiB limit')
            if (request.sample_rate, request.channels, request.encoding) != (16000, 1, 'pcm_s16le'):
                raise ValueError('Unsupported audio metadata')
            times = (request.capture_started_at, request.capture_ended_at, request.stamp)
            if any(t.sec < 0 or not 0 <= t.nanosec < 10**9 for t in times):
                raise ValueError('Invalid timestamp')
            start, end, sent = (t.sec * 10**9 + t.nanosec for t in times)
            if not 0 < start < end <= sent or end - start > 10**10:
                raise ValueError('Invalid capture time order or duration')
            with wave.open(str(path), 'rb') as wav:
                if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (16000, 1, 2, 'NONE'):
                    raise ValueError('Expected 16 kHz mono PCM16 WAV')
                frames = wav.getnframes()
                if not 0 < frames <= 160000:
                    raise ValueError('WAV duration must be in (0, 10] seconds')
                pcm = wav.readframes(frames)
                if len(pcm) != frames * 2 or abs((end - start) / 10**9 - frames / 16000) > 0.05:
                    raise ValueError('WAV frame count, data or capture duration mismatch')
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
