"""Persist actual model outputs beside their source recording."""

import json


def save_asr_text(audio_path, text):
    path = audio_path.with_suffix('.txt')
    path.write_text(text, encoding='utf-8')
    return path


def save_parse_result(audio_path, result):
    candidate = {name: getattr(result.candidate, name) for name in (
        'task_type', 'object_id', 'pickup_station_id', 'dropoff_station_id',
        'destination_station_id', 'return_required', 'return_station_id')}
    report = {name: getattr(result, name) for name in ('request_id', 'session_id', 'input_id', 'input_context_id')}
    report.update(success=result.success, outcome=result.outcome, message=result.message,
                  error_code=result.error_code, candidate=candidate,
                  stamp={'sec': result.stamp.sec, 'nanosec': result.stamp.nanosec},
                  missing_fields=list(result.missing_fields),
                  clarification_question=result.clarification_question)
    path = audio_path.with_suffix('.parse.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return path
