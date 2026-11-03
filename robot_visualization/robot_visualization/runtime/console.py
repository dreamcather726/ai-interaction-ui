"""将运行消息立即输出到启动终端，便于观察后台线程与子进程。"""

import threading
import re
from datetime import datetime

_OUTPUT_LOCK = threading.Lock()
LOG_PREFIXES = {"DEBUG": "[D]", "INFO": "[I]", "WARNING": "[W]", "ERROR": "[E]"}
_LOG_TAG = re.compile(r"(?:^|:\s)\[(D|I|W|E)\](?=\s|$)")
_ROS_LEVEL = re.compile(r"\[(DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\]")
_PREFIX_TAG = re.compile(r"^\[(D|I|W|E)\](?=\s|$)")
_EXCEPTION_LINE = re.compile(r"^[\w.]*(?:Error|Exception):")


def is_exception_line(message):
    return bool(_EXCEPTION_LINE.match(message))


def log_level(message, default="INFO"):
    """保留显式分级；对原有ROS输出识别级别和已知内部进度，不改原消息内容。"""
    text = str(message)
    tags = _LOG_TAG.search(text)
    if tags:
        return {"D": "DEBUG", "I": "INFO", "W": "WARNING", "E": "ERROR"}[tags[1]]
    ros = _ROS_LEVEL.search(text)
    level = ros[1] if ros else default.upper()
    level = {"WARN": "WARNING", "FATAL": "ERROR", "CRITICAL": "ERROR"}.get(level, level)
    if level == "ERROR":
        return level
    if is_exception_line(text) or any(
        word in text for word in ("Fatal Python error", "Segmentation fault", "Aborted")
    ):
        return "ERROR"
    if any(
        word in text
        for word in (
            "失败",
            "未成功",
            "异常",
            "崩溃",
            "不可用：",
            "拒绝语义请求",
            "Traceback",
        )
    ):
        return "ERROR" if "Traceback" not in text else "DEBUG"
    if level == "INFO" and any(
        word in text
        for word in (
            "识别完成，请求",
            "模型已就绪",
            "草稿已保存",
            "草稿已确认",
            "服务已在线",
        )
    ):
        return "INFO"
    if any(
        word in text
        for word in (
            "音频入队",
            "等待队列",
            "本阶段等待上限",
            "仍在等待",
            "开始识别，",
            "初始化仍在进行",
            "ASR 启动：",
            "识别文字已保存：",
            "继续澄清；",
            "开始新的任务理解",
            "已保存待补充候选",
            "候选字段完整且",
            "请求已发送",
            "通信线程已启动，正在发现",
            "已发现节点：",
            "处理界面操作：",
            "已发送录音请求：",
            "开始录音暂不可用：",
            "操作队列已满",
            "队列已满",
            "去重缓存",
            "已淘汰",
            "Future",
            "discard",
            "迟到结果",
            "收尾结束",
            "草稿编辑：编辑第",
            "界面代码：",
            "界面布局：",
            "Python=",
            "Loading ckpt:",
            "map: [",
            "excludes:",
            "Ollama 请求参数",
            "Ollama 响应",
            "正在导入 FunASR",
            "FunASR 依赖导入成功",
            "语音识别片段",
            "funasr version:",
        )
    ):
        return "DEBUG"
    return level if level in LOG_PREFIXES else "INFO"


def has_log_level(message):
    """区分新日志记录与 traceback 的续行。"""
    return bool(_LOG_TAG.search(message) or _ROS_LEVEL.search(message))


def format_log(message, level="INFO"):
    """每个物理行都带前缀，保持正文和空行；已经分级的消息不重复加前缀。"""
    level = {"WARN": "WARNING", "FATAL": "ERROR", "CRITICAL": "ERROR"}.get(
        level.upper(), level.upper()
    )
    prefix = LOG_PREFIXES.get(level, LOG_PREFIXES["INFO"])
    lines = []
    traceback_mode = False
    for line in str(message).split("\n"):
        if level == "ERROR" and line.lstrip().startswith(
            "Traceback (most recent call last):"
        ):
            traceback_mode = True
        line_prefix = "[D]" if traceback_mode else prefix
        if traceback_mode and is_exception_line(line):
            line_prefix = "[E]"
        lines.append(line if _PREFIX_TAG.match(line) else f"{line_prefix} {line}")
    return "\n".join(lines)


def console_log(message):
    """整条消息加时间输出；管道重定向时也不等待缓冲区填满。"""
    with _OUTPUT_LOCK:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"[{stamp}] [工作台] {message}", flush=True)
