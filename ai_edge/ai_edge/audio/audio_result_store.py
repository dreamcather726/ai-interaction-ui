"""将模型实际返回的识别文字和语义结果保存到录音文件旁。"""

import json


def save_asr_text(audio_path, text):
    """仅保存实际 ASR 文字，不生成或替换识别答案。"""
    path = audio_path.with_suffix(".txt")
    path.write_text(text, encoding="utf-8")
    return path


def save_parse_result(audio_path, result):
    """保存语义结果、来源关联和候选字段，便于人工核对。"""
    candidate = {
        name: getattr(result.candidate, name)
        for name in (
            "task_type",
            "object_id",
            "pickup_station_id",
            "dropoff_station_id",
            "destination_station_id",
            "return_required",
            "return_station_id",
        )
    }
    candidate["steps"] = [
        {name: getattr(step, name) for name in candidate if name != "steps"}
        for step in result.candidate.steps
    ]
    report = {
        name: getattr(result, name)
        for name in ("request_id", "session_id", "input_id", "input_context_id")
    }
    report.update(
        success=result.success,
        outcome=result.outcome,
        message=result.message,
        error_code=result.error_code,
        candidate=candidate,
        stamp={"sec": result.stamp.sec, "nanosec": result.stamp.nanosec},
        missing_fields=list(result.missing_fields),
        clarification_question=result.clarification_question,
    )
    path = audio_path.with_suffix(".parse.json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
