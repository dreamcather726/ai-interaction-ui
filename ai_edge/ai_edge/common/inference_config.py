"""Inference defaults and validation shared by standalone nodes and the dashboard."""

import math
from urllib.parse import urlsplit

LLM_DEFAULTS = {
    "model": "qwen3.5:2b",
    "ollama_url": "http://127.0.0.1:11434",
    "request_timeout": 290.0,
    "temperature": 0.0,
    "num_ctx": 32768,
    "num_predict": 16000,
    "keep_alive": "5m",
    "robot_query_timeout": 3.0,
}

ASR_DEFAULTS = {
    "asr_device": "cpu",
    "asr_language": "zh",
    "asr_ncpu": 4,
    "asr_recognition_timeout": 180.0,
}

PIPELINE_DEFAULTS = {
    "asr_timeout": 185.0,
    "parse_timeout": 300.0,
    "clarification_timeout": 300.0,
}


def bounded_number(name, value, minimum, maximum, *, integer=False):
    kind = int if integer else (int, float)
    if (
        isinstance(value, bool)
        or not isinstance(value, kind)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(
            f"{name} must be in [{minimum}, {maximum}]"
            + (" and an integer" if integer else "")
        )


def validate_llm(values):
    for name in ("model", "keep_alive"):
        if not isinstance(values[name], str) or not values[name].strip():
            raise ValueError(f"{name} must not be empty")
    parts = urlsplit(values["ollama_url"])
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise ValueError("ollama_url must be an HTTP/HTTPS URL without credentials")
    # Reading port also rejects invalid port values at startup.
    _ = parts.port
    bounded_number("request_timeout", values["request_timeout"], 0.1, 290)
    bounded_number("temperature", values["temperature"], 0, 2)
    bounded_number("num_ctx", values["num_ctx"], 1024, 131072, integer=True)
    bounded_number("num_predict", values["num_predict"], 1, 65536, integer=True)
    bounded_number("robot_query_timeout", values["robot_query_timeout"], 0.1, 30)
    if values["num_predict"] >= values["num_ctx"]:
        raise ValueError("num_predict must be smaller than num_ctx")


def validate_pipeline(values):
    validate_llm(values)
    for name in PIPELINE_DEFAULTS:
        bounded_number(name, values[name], 0.1, 300)
    bounded_number(
        "asr_recognition_timeout", values["asr_recognition_timeout"], 0.1, 300
    )
    bounded_number("asr_ncpu", values["asr_ncpu"], 1, 256, integer=True)
    for name in ("asr_device", "asr_language"):
        if not isinstance(values[name], str) or not values[name].strip():
            raise ValueError(f"{name} must not be empty")
    if values["asr_timeout"] <= values["asr_recognition_timeout"]:
        raise ValueError("asr_timeout must exceed asr_recognition_timeout")
    if values["parse_timeout"] <= (
        values["request_timeout"] + 3 * values["robot_query_timeout"]
    ):
        raise ValueError(
            "parse_timeout must exceed request_timeout + 3 * robot_query_timeout"
        )
