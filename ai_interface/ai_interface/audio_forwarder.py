"""Package saved audio and submit it to ai_edge; no recognition logic."""

from collections import deque
import time

from rclpy.clock import Clock, ClockType
from robot_interfaces.msg import RequestStatus
from robot_interfaces.srv import ProcessAudio


class AudioForwarder:
    """Only deliver audio file metadata and report AI admission status."""

    def __init__(self, node):
        self.node = node
        node.declare_parameter('auto_forward', True)
        self.client = node.create_client(ProcessAudio, '/ai/process_audio')
        self.queue = deque()
        self.active = None
        self.clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = node.create_timer(0.05, self.tick, clock=self.clock)

    def report(self, request_id, code, message):
        result = RequestStatus()
        result.stamp = self.node.get_clock().now().to_msg()
        result.request_id = request_id
        result.source_node = self.node.get_fully_qualified_name()
        result.operation = '/ai/process_audio'
        result.stage = RequestStatus.REJECTED
        result.message, result.error_code = message, code
        self.node.receipt_pub.publish(result)
        self.node.get_logger().warning(f'{code}: {message}')

    def enqueue(self, msg, path):
        if not self.node.get_parameter('auto_forward').value:
            return
        if len(self.queue) + (self.active is not None) >= 10:
            self.report(msg.request_id, 'FORWARD_QUEUE_FULL', 'Audio saved; delivery queue is full')
            return
        request = ProcessAudio.Request()
        for field in ('stamp', 'request_id', 'session_id', 'segment_id', 'input_context_id',
                      'capture_started_at', 'capture_ended_at', 'sample_rate', 'channels', 'encoding'):
            setattr(request, field, getattr(msg, field))
        request.audio_path = str(path.resolve())
        self.queue.append(request)

    def tick(self):
        if self.active is None:
            if not self.queue:
                return
            self.active = {'request': self.queue.popleft(), 'deadline': time.monotonic() + 3}
        job = self.active
        request = job['request']
        try:
            if time.monotonic() >= job['deadline']:
                code = 'DEPENDENCY_UNAVAILABLE'
                if 'future' in job:
                    self.client.remove_pending_request(job['future'])
                    job['future'].cancel()
                    code = 'FORWARD_TIMEOUT'
                self.report(request.request_id, code,
                            'AI handoff unconfirmed; file retained, no automatic retry')
                self.active = None
            elif 'future' not in job:
                if self.client.service_is_ready():
                    job['future'] = self.client.call_async(request)
                    job['deadline'] = time.monotonic() + 3
            elif job['future'].done():
                receipt = job['future'].result().receipt
                if (receipt.request_id != request.request_id
                        or receipt.operation != '/ai/process_audio'
                        or not receipt.message.strip()
                        or receipt.stage != (RequestStatus.RECEIVED if receipt.success else RequestStatus.REJECTED)
                        or (receipt.success and receipt.error_code)
                        or (not receipt.success and not receipt.error_code)):
                    self.report(request.request_id, 'INVALID_AI_RECEIPT', 'Invalid AI admission receipt')
                else:
                    self.node.get_logger().info(
                        f'AI handoff request_id={request.request_id}: {receipt.message}')
                # AI service publishes its own receipt; do not publish it twice.
                self.active = None
        except Exception as exc:
            self.report(request.request_id, 'FORWARD_FAILED', str(exc))
            self.active = None

    def close(self):
        self.timer.cancel()
        self.queue.clear()
        if self.active is not None and 'future' in self.active:
            self.client.remove_pending_request(self.active['future'])
            self.active['future'].cancel()
        self.active = None
