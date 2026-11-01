"""Capture a real PulseAudio microphone with parecord and publish PCM."""

import os
import shutil
import signal
import subprocess
import tempfile
import time
from uuid import uuid4

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from robot_interfaces.msg import AudioSegment
from std_srvs.srv import Trigger


class MicrophoneSenderNode(Node):
    """Provide recording controls using the same interface as the simulator."""

    def __init__(self):
        super().__init__('microphone_sender_node')
        self.declare_parameter('device', '@DEFAULT_SOURCE@')
        self.declare_parameter('session_id', 'mic-session-' + uuid4().hex)
        self.declare_parameter('input_context_id', 'mic-context-' + uuid4().hex)
        self.device = self.get_parameter('device').value
        self.process = None
        self.errors = None
        self.pcm = bytearray()
        self.started_at = None
        self.session_id = ''
        self.context_id = ''
        self.publisher = self.create_publisher(AudioSegment, '/audio/segments', 10)
        self.start_service = self.create_service(
            Trigger, '/audio/start_recording', self.start_recording)
        self.stop_service = self.create_service(
            Trigger, '/audio/stop_recording', self.stop_recording)
        self.timer_clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = self.create_timer(
            0.05, self.poll_recording, clock=self.timer_clock)
        self.get_logger().info(
            f'Real microphone ready for requests: device={self.device}')

    def start_recording(self, request, response):
        """Launch capture without blocking the ROS executor."""
        if self.process is not None:
            response.success = self.process.poll() is None
            response.message = 'Recording already active' if response.success else (
                'AUDIO_DEVICE_UNAVAILABLE: previous capture exited; stop first')
            return response
        executable = shutil.which('parecord')
        if executable is None:
            response.message = 'AUDIO_DEVICE_UNAVAILABLE: install pulseaudio-utils'
            return response
        self.session_id = self.get_parameter('session_id').value.strip()
        self.context_id = self.get_parameter('input_context_id').value.strip()
        if not self.session_id or not self.context_id:
            response.message = 'INVALID_REQUEST: session/context must be nonempty'
            return response
        if self.get_clock().now().nanoseconds <= 0:
            response.message = 'INVALID_REQUEST: ROS clock not ready'
            return response
        try:
            self.errors = tempfile.TemporaryFile()
            self.process = subprocess.Popen(
                [executable, '--raw', '--format=s16le', '--rate=16000',
                 '--channels=1', '--latency-msec=50', '--device=' + self.device],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=self.errors)
            os.set_blocking(self.process.stdout.fileno(), False)
        except (OSError, ValueError) as exc:
            self.abort_recording()
            response.message = f'AUDIO_DEVICE_UNAVAILABLE: {exc}'
            return response
        self.pcm.clear()
        self.started_at = time.monotonic()
        response.success = True
        response.message = 'Capture process started; awaiting microphone PCM'
        return response

    def drain_audio(self):
        """Drain the pipe regularly so parecord cannot block on a full pipe."""
        while self.process is not None:
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            remaining = 320000 - len(self.pcm)
            self.pcm.extend(chunk[:remaining])
            if len(self.pcm) >= 320000:
                break

    def poll_recording(self):
        """Collect bytes and enforce the ten-second maximum."""
        if self.process is None:
            return
        self.drain_audio()
        if (self.process.poll() is not None or len(self.pcm) >= 320000
                or time.monotonic() - self.started_at >= 10):
            result = self.stop_recording(None, Trigger.Response())
            if result.success:
                self.get_logger().info(result.message)
            else:
                self.get_logger().error(result.message)

    def close_capture(self):
        """Stop the process, drain remaining bytes and release resources."""
        process = self.process
        if process is None:
            return b'', '', 0
        try:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                remaining, _ = process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=1)
                raise RuntimeError('Capture did not stop normally; audio discarded')
            self.errors.seek(0)
            error = self.errors.read(4096).decode(errors='replace').strip()
            return remaining or b'', error, process.returncode
        finally:
            if process.stdout is not None:
                process.stdout.close()
            self.errors.close()
            self.process = None
            self.errors = None
            self.started_at = None

    def stop_recording(self, request, response):
        """Always stop capture; publish only complete valid PCM frames."""
        if self.process is None:
            response.success = True
            response.message = 'Already stopped; no audio republished'
            return response
        try:
            remaining, error, code = self.close_capture()
            self.pcm.extend(remaining[:320000 - len(self.pcm)])
            if code not in (0, -signal.SIGINT):
                raise RuntimeError(error or f'parecord exited with code {code}')
            if not self.pcm or len(self.pcm) % 2:
                raise RuntimeError('No complete PCM audio; check microphone access')
            if self.publisher.get_subscription_count() == 0:
                raise RuntimeError('Recording stopped but no audio receiver; discarded')
            now = self.get_clock().now().nanoseconds
            start = now - len(self.pcm) // 2 * 62500
            if start <= 0:
                raise RuntimeError('ROS clock is not ready; audio discarded')
            msg = AudioSegment()
            msg.request_id = 'mic-request-' + uuid4().hex
            msg.segment_id = 'mic-segment-' + uuid4().hex
            msg.session_id, msg.input_context_id = self.session_id, self.context_id
            msg.capture_started_at.sec, msg.capture_started_at.nanosec = divmod(start, 10**9)
            msg.capture_ended_at.sec, msg.capture_ended_at.nanosec = divmod(now, 10**9)
            msg.stamp.sec, msg.stamp.nanosec = divmod(now, 10**9)
            msg.sample_rate, msg.channels, msg.encoding = 16000, 1, 'pcm_s16le'
            msg.data = bytes(self.pcm)
            self.publisher.publish(msg)
            response.success = True
            response.message = f'Published {len(self.pcm)} PCM bytes, request_id={msg.request_id}'
        except (OSError, RuntimeError) as exc:
            response.message = f'AUDIO_CAPTURE_FAILED: {exc}'
        finally:
            self.pcm.clear()
        return response

    def abort_recording(self):
        """Discard audio when shutting down; never publish from cleanup."""
        try:
            self.close_capture()
        except (OSError, RuntimeError) as exc:
            self.get_logger().error(str(exc))
        finally:
            if self.errors is not None:
                self.errors.close()
                self.errors = None
            self.pcm.clear()


def main(args=None):
    """Run real microphone capture until interrupted."""
    rclpy.init(args=args)
    node = None
    try:
        node = MicrophoneSenderNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.abort_recording()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
