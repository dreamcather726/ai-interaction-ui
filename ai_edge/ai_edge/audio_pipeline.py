"""AI-side orchestration of ASR and semantic parsing for submitted files."""

import time

from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rclpy.parameter import Parameter
from robot_interfaces.action import ParseTask
from robot_interfaces.msg import RecognizedText, RequestStatus
from robot_interfaces.srv import RecognizeAudio
from ai_edge.audio_job_queue import AudioJobQueue
from ai_edge.audio_result_validation import (
    ResultValidationError, validate_asr_result, validate_parse_result)
from ai_edge.audio_result_store import save_asr_text, save_parse_result


class AudioPipeline:
    """Process one recording at a time without blocking audio reception."""

    def __init__(self, node, publish_receipt):
        self.node = node
        self.publish_receipt = publish_receipt
        node.declare_parameter('asr_timeout', 30.0)
        node.declare_parameter('parse_timeout', 30.0)
        node.declare_parameter('allowed_station_ids', Parameter.Type.STRING_ARRAY)
        node.declare_parameter('allowed_object_ids', Parameter.Type.STRING_ARRAY)
        for name in ('asr_timeout', 'parse_timeout'):
            value = node.get_parameter(name).value
            if not 0 < value <= 300:
                raise ValueError(f'{name} must be in (0, 300] seconds')
        self.asr = node.create_client(RecognizeAudio, '/ai/recognize_audio')
        self.parser = ActionClient(node, ParseTask, '/ai/parse_task')
        self.text_pub = node.create_publisher(RecognizedText, '/ai/asr_text', 10)
        self.queue = AudioJobQueue()
        self.active = None
        self.clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = node.create_timer(0.05, self.tick, clock=self.clock)

    def report(self, job, operation, success, message, code=''):
        receipt = RequestStatus()
        receipt.stamp = self.node.get_clock().now().to_msg()
        receipt.request_id = job['msg'].request_id
        receipt.source_node = self.node.get_fully_qualified_name()
        receipt.operation = operation
        receipt.success = success
        receipt.stage = RequestStatus.RECEIVED if success else RequestStatus.REJECTED
        receipt.message, receipt.error_code = message, code
        self.publish_receipt(receipt)
        text = f'{operation} request_id={receipt.request_id}: {code} {message}'
        if success:
            self.node.get_logger().info(text)
        else:
            self.node.get_logger().warning(text)

    def enqueue(self, msg, path, pcm):
        return self.queue.enqueue(msg, path, pcm, active=self.active is not None)

    def stage(self, name, timeout):
        self.active['stage'] = name
        self.active['deadline'] = time.monotonic() + timeout

    def fail(self, code, message):
        job = self.active
        operation = '/ai/parse_task' if job['stage'].startswith('parse') else '/ai/recognize_audio'
        self.report(job, operation, False, message, code)
        self.active = None

    @staticmethod
    def cancel_late_goal(future):
        """A late acceptance must not continue a timed-out parse silently."""
        try:
            handle = future.result()
            if handle.accepted:
                handle.cancel_goal_async()
        except Exception:
            pass

    def tick(self):
        try:
            self.advance()
        except Exception as exc:
            if self.active is not None:
                if self.active.get('handle') is not None:
                    self.active['handle'].cancel_goal_async()
                self.fail('PIPELINE_FAILED', str(exc))
            else:
                self.node.get_logger().error(f'PIPELINE_FAILED: {exc}')

    def advance(self):
        if self.active is None:
            if not self.queue:
                return
            self.active = self.queue.popleft()
            self.stage('asr_wait', 3.0)
        job = self.active
        stage = job['stage']
        if time.monotonic() >= job['deadline']:
            if stage == 'asr_result':
                self.asr.remove_pending_request(job['future'])
                job['future'].cancel()
            elif stage == 'parse_goal':
                job['future'].add_done_callback(self.cancel_late_goal)
            elif stage == 'parse_result':
                job['handle'].cancel_goal_async()
            code = 'DEPENDENCY_UNAVAILABLE' if stage.endswith('wait') else (
                'PARSE_TIMEOUT' if stage.startswith('parse') else 'ASR_TIMEOUT')
            self.fail(code, 'AI phase timed out; result unknown, no automatic retry')
            return
        if stage == 'asr_wait' and self.asr.service_is_ready():
            msg = job['msg']
            request = RecognizeAudio.Request()
            request.stamp = self.node.get_clock().now().to_msg()
            for field in ('request_id', 'session_id', 'segment_id', 'input_context_id',
                          'capture_started_at', 'capture_ended_at'):
                setattr(request, field, getattr(msg, field))
            request.audio_path = str(job['path'])
            job['future'] = self.asr.call_async(request)
            self.stage('asr_result', self.node.get_parameter('asr_timeout').value)
        elif stage == 'asr_result' and job['future'].done():
            self.accept_asr(job['future'].result().result)
        elif stage == 'parse_wait' and self.parser.server_is_ready():
            goal = ParseTask.Goal()
            msg = job['msg']
            goal.stamp = self.node.get_clock().now().to_msg()
            goal.request_id = msg.request_id
            goal.session_id = msg.session_id
            goal.input_id = 'audio:' + msg.segment_id
            goal.input_context_id = msg.input_context_id
            goal.text = job['text']
            goal.allowed_station_ids = self.node.get_parameter_or('allowed_station_ids').value or []
            goal.allowed_object_ids = self.node.get_parameter_or('allowed_object_ids').value or []
            job['future'] = self.parser.send_goal_async(goal)
            self.stage('parse_goal', 3.0)
        elif stage == 'parse_goal' and job['future'].done():
            handle = job['future'].result()
            if not handle.accepted:
                self.fail('PARSE_GOAL_REJECTED', 'AI rejected parsing; see AI RequestStatus for reason')
                return
            job['handle'] = handle
            job['future'] = handle.get_result_async()
            self.stage('parse_result', self.node.get_parameter('parse_timeout').value)
        elif stage == 'parse_result' and job['future'].done():
            self.accept_parse(job['future'].result())

    def accept_asr(self, result):
        job = self.active
        try:
            valid = validate_asr_result(job['msg'], result)
        except ResultValidationError as exc:
            self.fail(exc.code, str(exc))
            return
        self.text_pub.publish(result)
        if not valid:
            self.fail(result.error_code, result.message)
            return
        save_asr_text(job['path'], result.text)
        job['text'] = result.text
        self.report(job, '/ai/recognize_audio', True, result.text)
        self.stage('parse_wait', 3.0)

    def accept_parse(self, wrapped):
        job = self.active
        stations = self.node.get_parameter_or('allowed_station_ids').value or []
        objects = self.node.get_parameter_or('allowed_object_ids').value or []
        try:
            ready = validate_parse_result(job['msg'], wrapped, stations, objects)
        except ResultValidationError as exc:
            self.fail(exc.code, str(exc))
            return
        result = wrapped.result
        path = save_parse_result(job['path'], result)
        self.report(job, '/ai/parse_task', ready,
                    f'{result.message}; result={path}', result.error_code)
        self.active = None

    def close(self):
        """Stop local work and request cancellation of an active parse."""
        self.timer.cancel()
        if not self.node.context.ok():
            self.queue.clear()
            return
        if self.active is not None:
            if self.active.get('handle') is not None:
                self.active['handle'].cancel_goal_async()
            elif self.active.get('stage') == 'parse_goal':
                self.active['future'].add_done_callback(self.cancel_late_goal)
        self.queue.clear()
