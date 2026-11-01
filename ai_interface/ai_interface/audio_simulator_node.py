"""Simulate Orange Pi PCM audio output without a microphone."""

from array import array
import math
import sys
import time
from uuid import uuid4

import rclpy
from rclpy.node import Node
from robot_interfaces.msg import AudioSegment
from std_srvs.srv import Trigger


class AudioSimulatorNode(Node):
    """Send a test tone once, or simulate start/stop recording services."""

    def __init__(self):
        super().__init__('audio_simulator_node')
        self.declare_parameter('auto_send', True)
        self.declare_parameter('duration', 1.0)
        self.declare_parameter('frequency', 440.0)
        self.duration = float(self.get_parameter('duration').value)
        self.frequency = float(self.get_parameter('frequency').value)
        if not math.isfinite(self.duration) or not 0.05 <= self.duration <= 10:
            raise ValueError('duration must be between 0.05 and 10 seconds')
        if not math.isfinite(self.frequency) or not 20 <= self.frequency < 8000:
            raise ValueError('frequency must be between 20 and 8000 Hz (exclusive)')
        self.auto_pending = bool(self.get_parameter('auto_send').value)
        self.recording_since = None
        self.session_id = 'sim-session-' + uuid4().hex
        self.context_id = 'sim-context-' + uuid4().hex
        self.publisher = self.create_publisher(AudioSegment, '/audio/segments', 10)
        self.start_service = self.create_service(
            Trigger, '/audio/start_recording', self.start_recording)
        self.stop_service = self.create_service(
            Trigger, '/audio/stop_recording', self.stop_recording)
        self.timer = self.create_timer(0.1, self.tick)
        self.get_logger().info(
            'Simulator ready: synthetic tone, not speech or microphone input. '
            f'auto_send={self.auto_pending}')

    def publish_tone(self, duration):
        """Publish one complete int16 little-endian mono PCM segment."""
        frame_count = max(1, min(160000, round(duration * 16000)))
        samples = array('h', (
            round(6000 * math.sin(2 * math.pi * self.frequency * i / 16000))
            for i in range(frame_count)))
        if sys.byteorder != 'little':
            samples.byteswap()
        now = self.get_clock().now().nanoseconds
        start = now - round(frame_count / 16000 * 1000000000)
        if start <= 0:
            raise ValueError('ROS clock is not ready for a valid capture timestamp')
        msg = AudioSegment()
        msg.request_id = 'sim-request-' + uuid4().hex
        msg.segment_id = 'sim-segment-' + uuid4().hex
        msg.session_id = self.session_id
        msg.input_context_id = self.context_id
        msg.stamp.sec, msg.stamp.nanosec = divmod(now, 1000000000)
        msg.capture_ended_at.sec, msg.capture_ended_at.nanosec = divmod(now, 1000000000)
        msg.capture_started_at.sec, msg.capture_started_at.nanosec = divmod(start, 1000000000)
        msg.sample_rate, msg.channels, msg.encoding = 16000, 1, 'pcm_s16le'
        msg.data = samples.tobytes()
        self.publisher.publish(msg)
        self.get_logger().info(
            f'Sent request_id={msg.request_id}, frames={frame_count}, '
            f'bytes={len(msg.data)}')

    def start_recording(self, request, response):
        """Start timing a simulated capture; repeated starts keep the buffer."""
        self.auto_pending = False
        response.success = True
        if self.recording_since is None:
            self.recording_since = time.monotonic()
            response.message = 'Simulated recording started (test tone)'
        else:
            response.message = 'Simulated recording is already active'
        return response

    def stop_recording(self, request, response):
        """Publish once on stop; an idle stop never replays an old segment."""
        if self.recording_since is None:
            response.success = True
            response.message = 'Already stopped; no new audio published'
            return response
        if self.publisher.get_subscription_count() == 0:
            response.success = False
            response.message = 'No audio subscriber; start the receiver first'
            return response
        duration = min(10.0, time.monotonic() - self.recording_since)
        try:
            self.publish_tone(duration)
        except ValueError as exc:
            response.success = False
            response.message = str(exc)
            return response
        self.recording_since = None
        response.success = True
        response.message = 'Simulated recording stopped; test audio published'
        return response

    def tick(self):
        """Wait for discovery before the one-shot send; cap captures at 10 s."""
        if self.recording_since is not None:
            if time.monotonic() - self.recording_since >= 10:
                # Stop timing even if no receiver exists at the safety limit.
                self.stop_recording(None, Trigger.Response())
                self.recording_since = None
        elif self.auto_pending and self.publisher.get_subscription_count() > 0:
            try:
                self.publish_tone(self.duration)
            except ValueError as exc:
                self.get_logger().warning(str(exc))
            else:
                self.auto_pending = False


def main(args=None):
    """Run the simulator until interrupted."""
    rclpy.init(args=args)
    node = None
    try:
        node = AudioSimulatorNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
