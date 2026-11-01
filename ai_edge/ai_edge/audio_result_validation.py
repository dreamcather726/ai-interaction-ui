"""Pure ASR/semantic result checks: no ROS clients, queues or file writes."""

from action_msgs.msg import GoalStatus
from robot_interfaces.action import ParseTask
from robot_interfaces.msg import RecognizedText, TaskCandidate


class ResultValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_asr_result(msg, result):
    fields = ('request_id', 'session_id', 'segment_id', 'input_context_id',
              'capture_started_at', 'capture_ended_at')
    if any(getattr(result, f) != getattr(msg, f) for f in fields):
        raise ResultValidationError('ASR_RESULT_MISMATCH', 'ASR result identifiers/times do not match input')
    times = (result.recognized_at, result.stamp)
    if any(t.sec < 0 or not 0 <= t.nanosec < 10**9 for t in times):
        raise ResultValidationError('INVALID_ASR_RESULT', 'Invalid recognition timestamp')
    recognized, published = (t.sec * 10**9 + t.nanosec for t in times)
    captured = msg.capture_ended_at.sec * 10**9 + msg.capture_ended_at.nanosec
    if not captured <= recognized <= published or not (
            result.confidence == -1 or 0 <= result.confidence <= 1):
        raise ResultValidationError('INVALID_ASR_RESULT', 'Invalid recognition time order or confidence')
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
        raise ResultValidationError('INVALID_ASR_RESULT', 'ASR status/success/text/error fields are inconsistent')
    return bool(valid)


def validate_parse_result(msg, wrapped, stations, objects):
    result = wrapped.result
    expected = (msg.request_id, msg.session_id, 'audio:' + msg.segment_id, msg.input_context_id)
    actual = (result.request_id, result.session_id, result.input_id, result.input_context_id)
    if actual != expected:
        raise ResultValidationError('PARSE_RESULT_MISMATCH', 'Parsing result does not match source input')
    ready = (wrapped.status == GoalStatus.STATUS_SUCCEEDED and result.success
             and result.outcome == ParseTask.Result.READY and not result.error_code
             and not result.missing_fields and not result.clarification_question)
    if ready and not valid_candidate(result.candidate, stations, objects):
        raise ResultValidationError('INVALID_MODEL_OUTPUT', 'Candidate violates task fields or configured whitelist')
    clarification = (wrapped.status == GoalStatus.STATUS_SUCCEEDED and not result.success
                     and result.outcome == ParseTask.Result.NEED_CLARIFICATION
                     and result.error_code == 'MISSING_FIELDS' and result.missing_fields
                     and result.clarification_question.strip())
    terminal_failure = (not result.success and bool(result.error_code) and (
        (wrapped.status == GoalStatus.STATUS_ABORTED and result.outcome in (
            ParseTask.Result.UNSUPPORTED, ParseTask.Result.FAILED)) or
        (wrapped.status == GoalStatus.STATUS_CANCELED and result.outcome == ParseTask.Result.CANCELED)))
    if not result.message.strip() or not (ready or clarification or terminal_failure):
        raise ResultValidationError('INVALID_MODEL_OUTPUT', 'Action status and semantic result are inconsistent')
    return bool(ready)


def valid_candidate(candidate, stations, objects):
    stations, objects = set(stations), set(objects)
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
