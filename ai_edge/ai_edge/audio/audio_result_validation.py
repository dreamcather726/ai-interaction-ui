"""校验 ASR、语义结果和任务候选，不创建客户端、不操作队列或文件。"""

from action_msgs.msg import GoalStatus

from ai_edge.common import error_codes as Codes
from ai_edge.semantic.semantic_contract import (
    SemanticError,
    candidate_payload,
    validate_candidate,
)
from robot_interfaces.action import ParseTask
from robot_interfaces.msg import RecognizedText


class ResultValidationError(ValueError):
    """携带稳定错误码的业务校验异常，交给上层统一报告。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_asr_result(msg, result):
    """检查来源和状态一致性；返回是否有可进入解析的有效文字。"""
    fields = (
        "request_id",
        "session_id",
        "segment_id",
        "input_context_id",
        "capture_started_at",
        "capture_ended_at",
    )
    if any(getattr(result, f) != getattr(msg, f) for f in fields):
        raise ResultValidationError(
            Codes.ASR_RESULT_MISMATCH, "ASR 结果的来源标识或录音时间与请求不一致"
        )
    times = (result.recognized_at, result.stamp)
    if any(t.sec < 0 or not 0 <= t.nanosec < 10**9 for t in times):
        raise ResultValidationError(
            Codes.INVALID_ASR_RESULT, "识别结果时间戳格式不正确"
        )
    recognized, published = (t.sec * 10**9 + t.nanosec for t in times)
    captured = msg.capture_ended_at.sec * 10**9 + msg.capture_ended_at.nanosec
    if not captured <= recognized <= published or not (
        result.confidence == -1 or 0 <= result.confidence <= 1
    ):
        raise ResultValidationError(
            Codes.INVALID_ASR_RESULT, "识别结果的时间顺序或置信度不符合约定"
        )
    valid = (
        result.status == RecognizedText.SUCCESS
        and result.success
        and bool(result.text.strip())
        and len(result.text) <= 16384
        and not result.error_code
        and bool(result.message.strip())
    )
    failed = (
        not result.success
        and not result.text
        and bool(result.error_code)
        and bool(result.message.strip())
        and (
            result.status == RecognizedText.FAILED
            or (
                result.status == RecognizedText.NO_SPEECH
                and result.error_code == Codes.NO_SPEECH
            )
        )
    )
    if not valid and not failed:
        raise ResultValidationError(
            Codes.INVALID_ASR_RESULT, "ASR 状态、成功标志、文本和错误字段相互矛盾"
        )
    return bool(valid)


def validate_parse_result(msg, wrapped, stations, objects):
    """检查 Action 终态及语义结果；返回是否得到完整有效候选。"""
    result = wrapped.result
    expected = (
        msg.request_id,
        msg.session_id,
        "audio:" + msg.segment_id,
        msg.input_context_id,
    )
    actual = (
        result.request_id,
        result.session_id,
        result.input_id,
        result.input_context_id,
    )
    if actual != expected:
        raise ResultValidationError(
            Codes.PARSE_RESULT_MISMATCH, "语义解析结果与来源输入不一致"
        )
    ready = (
        wrapped.status == GoalStatus.STATUS_SUCCEEDED
        and result.success
        and result.outcome == ParseTask.Result.READY
        and not result.error_code
        and not result.missing_fields
        and not result.clarification_question
    )
    if ready and not valid_candidate(result.candidate, stations, objects):
        raise ResultValidationError(
            Codes.INVALID_MODEL_OUTPUT, "任务候选字段或工位、物料白名单校验未通过"
        )
    clarification = (
        wrapped.status == GoalStatus.STATUS_SUCCEEDED
        and not result.success
        and result.outcome == ParseTask.Result.NEED_CLARIFICATION
        and result.error_code == Codes.MISSING_FIELDS
        and result.missing_fields
        and result.clarification_question.strip()
    )
    if clarification:
        try:
            checked = validate_candidate(
                candidate_payload(result.candidate), stations, objects
            )
        except SemanticError as exc:
            raise ResultValidationError(Codes.INVALID_MODEL_OUTPUT, str(exc)) from exc
        if checked.unsupported or list(checked.missing_fields) != list(
            result.missing_fields
        ):
            raise ResultValidationError(
                Codes.INVALID_MODEL_OUTPUT, "追问的缺失字段与候选实际内容不一致"
            )
    terminal_failure = (
        not result.success
        and bool(result.error_code)
        and (
            (
                wrapped.status == GoalStatus.STATUS_ABORTED
                and result.outcome
                in (ParseTask.Result.UNSUPPORTED, ParseTask.Result.FAILED)
            )
            or (
                wrapped.status == GoalStatus.STATUS_CANCELED
                and result.outcome == ParseTask.Result.CANCELED
            )
        )
    )
    if not result.message.strip() or not (ready or clarification or terminal_failure):
        raise ResultValidationError(
            Codes.INVALID_MODEL_OUTPUT, "Action 终态与语义解析结果不一致"
        )
    return bool(ready)


def valid_candidate(candidate, stations, objects):
    """按任务类型检查必填字段、互斥字段和配置白名单。"""
    try:
        answer = validate_candidate(candidate_payload(candidate), stations, objects)
        return not answer.unsupported and not answer.missing_fields
    except SemanticError:
        return False
