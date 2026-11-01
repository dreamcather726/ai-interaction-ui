"""AI-side orchestration of ASR and semantic parsing for submitted files."""

from collections import deque
import hashlib
import json
import time

from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rclpy.parameter import Parameter
from robot_interfaces.action import ParseTask
from robot_interfaces.msg import RecognizedText, RequestStatus, TaskCandidate
from robot_interfaces.srv import RecognizeAudio


class AudioPipeline:
    """Process one recording at a time without blocking audio reception."""

    def __init__(self, node):
        self.node = node
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
        self.queue = deque()
        self.active = None
        self.seen_requests = {}
        self.seen_inputs = {}
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
        self.node.receipt_pub.publish(receipt)
        text = f'{operation} request_id={receipt.request_id}: {code} {message}'
        if success:
            self.node.get_logger().info(text)
        else:
            self.node.get_logger().warning(text)

    def enqueue(self, msg, path, pcm):
        """Keep source context and reject replay conflicts before any AI call."""
        job = {'msg': msg, 'path': path.resolve()}
        # Hash semantic values, not CDR padding bytes, which may vary per receive.
        metadata = [getattr(msg, field) for field in (
            'request_id', 'session_id', 'segment_id', 'input_context_id',
            'sample_rate', 'channels', 'encoding')]
        metadata.extend((getattr(msg, field).sec, getattr(msg, field).nanosec)
                        for field in ('stamp', 'capture_started_at', 'capture_ended_at'))
        digest = hashlib.sha256(json.dumps(metadata).encode('utf-8'))
        digest.update(pcm)
        fingerprint = digest.hexdigest()
        key = (msg.session_id, msg.segment_id)
        for cache, identity, code in (
                (self.seen_requests, msg.request_id, 'REQUEST_ID_CONFLICT'),
                (self.seen_inputs, key, 'INPUT_ID_CONFLICT')):
            if identity in cache:
                same = cache[identity] == fingerprint
                return (same, '' if same else code,
                        'Duplicate input: already registered, not resubmitted' if same
                        else 'Input identity was reused with different content')
        if len(self.queue) + (self.active is not None) >= 10:
            return False, 'PIPELINE_BUSY', 'Audio saved; AI queue is full'
        if len(self.seen_requests) >= 1024:
            return False, 'RECOVERY_REQUIRED', 'Audio saved; deduplication cache is full'
        self.seen_requests[msg.request_id] = fingerprint
        self.seen_inputs[key] = fingerprint
        self.queue.append(job)
        return True, '', 'Audio accepted by AI; processing queued'

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
        msg = job['msg']
        fields = ('request_id', 'session_id', 'segment_id', 'input_context_id',
                  'capture_started_at', 'capture_ended_at')
        if any(getattr(result, f) != getattr(msg, f) for f in fields):
            self.fail('ASR_RESULT_MISMATCH', 'ASR result identifiers/times do not match input')
            return
        times = (result.recognized_at, result.stamp)
        if any(t.sec < 0 or not 0 <= t.nanosec < 10**9 for t in times):
            self.fail('INVALID_ASR_RESULT', 'Invalid recognition timestamp')
            return
        recognized, published = (t.sec * 10**9 + t.nanosec for t in times)
        captured = msg.capture_ended_at.sec * 10**9 + msg.capture_ended_at.nanosec
        if not captured <= recognized <= published or not (
                result.confidence == -1 or 0 <= result.confidence <= 1):
            self.fail('INVALID_ASR_RESULT', 'Invalid recognition time order or confidence')
            return
        valid = (
            result.status == RecognizedText.SUCCESS and result.success
            and bool(result.text.strip()) and len(result.text) <= 4096
            and not result.error_code and bool(result.message.strip()))
        failed = (
            not result.success and not result.text and bool(result.error_code)
            and bool(result.message.strip())
            and (result.status == RecognizedText.FAILED or (
                result.status == RecognizedText.NO_SPEECH and result.error_code == 'NO_SPEECH')))
        if not valid and not failed:
            self.fail('INVALID_ASR_RESULT', 'ASR status/success/text/error fields are inconsistent')
            return
        self.text_pub.publish(result)
        if not valid:
            self.fail(result.error_code, result.message)
            return
        job['path'].with_suffix('.txt').write_text(result.text, encoding='utf-8')
        job['text'] = result.text
        self.report(job, '/ai/recognize_audio', True, result.text)
        self.stage('parse_wait', 3.0)

    def accept_parse(self, wrapped):
        job = self.active
        result, msg = wrapped.result, job['msg']
        expected = (msg.request_id, msg.session_id, 'audio:' + msg.segment_id, msg.input_context_id)
        actual = (result.request_id, result.session_id, result.input_id, result.input_context_id)
        if actual != expected:
            self.fail('PARSE_RESULT_MISMATCH', 'Parsing result does not match source input')
            return
        ready = (wrapped.status == GoalStatus.STATUS_SUCCEEDED and result.success
                 and result.outcome == ParseTask.Result.READY and not result.error_code
                 and not result.missing_fields and not result.clarification_question)
        if ready and not self.valid_candidate(result.candidate):
            self.fail('INVALID_MODEL_OUTPUT', 'Candidate violates task fields or configured whitelist')
            return
        clarification = (wrapped.status == GoalStatus.STATUS_SUCCEEDED and not result.success
                         and result.outcome == ParseTask.Result.NEED_CLARIFICATION
                         and result.error_code == 'MISSING_FIELDS' and result.missing_fields
                         and result.clarification_question.strip())
        terminal_failure = (not result.success and bool(result.error_code) and (
            (wrapped.status == GoalStatus.STATUS_ABORTED and result.outcome in (
                ParseTask.Result.UNSUPPORTED, ParseTask.Result.FAILED)) or
            (wrapped.status == GoalStatus.STATUS_CANCELED and result.outcome == ParseTask.Result.CANCELED)))
        if not result.message.strip() or not (ready or clarification or terminal_failure):
            self.fail('INVALID_MODEL_OUTPUT', 'Action status and semantic result are inconsistent')
            return
        candidate = {name: getattr(result.candidate, name) for name in (
            'task_type', 'object_id', 'pickup_station_id', 'dropoff_station_id',
            'destination_station_id', 'return_required', 'return_station_id')}
        report = dict(zip(('request_id', 'session_id', 'input_id', 'input_context_id'), actual))
        report.update(success=result.success, outcome=result.outcome, message=result.message,
                      error_code=result.error_code, candidate=candidate,
                      stamp={'sec': result.stamp.sec, 'nanosec': result.stamp.nanosec},
                      missing_fields=list(result.missing_fields),
                      clarification_question=result.clarification_question)
        path = job['path'].with_suffix('.parse.json')
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        self.report(job, '/ai/parse_task', bool(ready),
                    f'{result.message}; result={path}', result.error_code)
        self.active = None

    def valid_candidate(self, candidate):
        stations = set(self.node.get_parameter_or('allowed_station_ids').value or [])
        objects = set(self.node.get_parameter_or('allowed_object_ids').value or [])
        if candidate.task_type == TaskCandidate.NAVIGATE:
            targets = [candidate.destination_station_id]
            valid = not any((candidate.object_id, candidate.pickup_station_id, candidate.dropoff_station_id))
        elif candidate.task_type == TaskCandidate.TRANSPORT:
            targets = [candidate.pickup_station_id, candidate.dropoff_station_id]
            valid = bool(candidate.object_id in objects) and not candidate.destination_station_id
        else:
            return False
        if candidate.return_required:
            targets.append(candidate.return_station_id)
        elif candidate.return_station_id:
            return False
        return valid and all(target and target in stations for target in targets)

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
