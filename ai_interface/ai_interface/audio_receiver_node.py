"""Receive complete PCM segments from the Orange Pi over ROS 2."""

from pathlib import Path
from uuid import uuid4
import wave

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile
from robot_interfaces.msg import AudioSegment, RequestStatus


class AudioReceiverNode(Node):
    """Validate incoming audio, save a WAV file and publish a receipt."""

    def __init__(self):
        super().__init__('audio_receiver_node')
        self.declare_parameter('output_dir', 'received_audio')
        self.output_dir = Path(
            self.get_parameter('output_dir').value).expanduser().resolve()
        # QoSProfile defaults: reliable, volatile, keep-last.
        qos = QoSProfile(depth=10)
        self.receipts = self.create_publisher(
            RequestStatus, '/ai/request_status', qos)
        self.subscription = self.create_subscription(
            AudioSegment, '/audio/segments', self.receive_audio, qos)
        self.get_logger().info(
            f'Listening on /audio/segments; WAV directory: {self.output_dir}')

    @staticmethod
    def validate(msg):
        """Return PCM bytes or raise ValueError for invalid input."""
        if not all(value.strip() for value in (
                msg.request_id, msg.session_id, msg.segment_id,
                msg.input_context_id)):
            raise ValueError('request/session/segment/context IDs must be nonempty')
        if (msg.encoding, msg.sample_rate, msg.channels) != (
                'pcm_s16le', 16000, 1):
            raise ValueError('Expected pcm_s16le, 16000 Hz, mono')
        pcm = bytes(msg.data)
        if not pcm or len(pcm) > 320000 or len(pcm) % 2:
            raise ValueError('PCM must contain 1..160000 complete int16 frames')
        times = (msg.capture_started_at, msg.capture_ended_at, msg.stamp)
        if any(t.sec < 0 or not 0 <= t.nanosec < 1000000000 for t in times):
            raise ValueError('Invalid ROS timestamp')
        start, end, stamp = (
            t.sec * 1000000000 + t.nanosec for t in times)
        if start <= 0 or end <= start or stamp < end:
            raise ValueError('Expected 0 < capture start < capture end <= stamp')
        duration = len(pcm) / 32000.0
        if end - start > 10000000000 or abs(
                (end - start) / 1000000000 - duration) > 0.05:
            raise ValueError('Capture duration does not match PCM frame count')
        return pcm

    def receive_audio(self, msg):
        """Save one segment; receipt success means storage, not ASR success."""
        receipt = RequestStatus()
        receipt.request_id = msg.request_id
        receipt.source_node = self.get_fully_qualified_name()
        receipt.operation = '/audio/segments'
        path = None
        try:
            pcm = self.validate(msg)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            # Never use sender-controlled IDs as filesystem paths.
            path = self.output_dir / f'{uuid4().hex}.wav'
            with wave.open(str(path), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(pcm)
            receipt.success = True
            receipt.stage = RequestStatus.RECEIVED
            receipt.message = f'Audio saved: {path} ({len(pcm)} PCM bytes)'
            self.get_logger().info(
                f'request_id={msg.request_id}: {receipt.message}')
        except (ValueError, OSError, wave.Error) as exc:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    self.get_logger().error(f'Partial WAV cleanup failed: {cleanup_error}')
            receipt.success = False
            receipt.stage = RequestStatus.REJECTED
            receipt.error_code = (
                'INVALID_REQUEST' if isinstance(exc, ValueError)
                else 'AUDIO_STORAGE_FAILED')
            receipt.message = str(exc)
            self.get_logger().error(
                f'request_id={msg.request_id}: {receipt.error_code}: {exc}')
        receipt.stamp = self.get_clock().now().to_msg()
        self.receipts.publish(receipt)


def main(args=None):
    """Run until stopped, keeping the receiver alive after bad input."""
    rclpy.init(args=args)
    node = AudioReceiverNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
