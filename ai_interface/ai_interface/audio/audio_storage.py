"""校验传输的 PCM 数据并保存带日期时间的 WAV 文件，不创建 ROS 节点。"""

import wave
from datetime import datetime, timedelta, timezone
from uuid import uuid4


def validate_audio_segment(msg):
    """校验 PCM 格式、来源标识和录音时长，返回原始字节。"""
    if msg.encoding != "pcm_s16le":
        raise ValueError("音频格式错误")

    if msg.sample_rate != 16000:
        raise ValueError("采样率必须为16000Hz")

    if msg.channels != 1:
        raise ValueError("必须使用单声道")

    pcm = bytes(msg.data)

    if len(pcm) == 0:
        raise ValueError("音频数据为空")

    if len(pcm) > 9600000:
        raise ValueError("音频长度超过300秒限制")

    if len(pcm) % 2 != 0:
        raise ValueError("PCM数据长度错误")

    if not all(
        value.strip()
        for value in (
            msg.request_id,
            msg.segment_id,
            msg.session_id,
            msg.input_context_id,
        )
    ):
        raise ValueError("音频请求、片段、会话和上下文ID不能为空")
    stamps = (msg.capture_started_at, msg.capture_ended_at, msg.stamp)
    if any(t.sec < 0 or not 0 <= t.nanosec < 10**9 for t in stamps):
        raise ValueError("音频时间戳非法")
    start, end, sent = (t.sec * 10**9 + t.nanosec for t in stamps)
    if not 0 < start < end <= sent or end - start > 300 * 10**9:
        raise ValueError("录音起止时间无效或超过300秒")
    if abs((end - start) / 10**9 - len(pcm) / 32000) > 0.05:
        raise ValueError("录音时长与音频帧数不一致")

    return pcm


def save_wav(output_dir, pcm):
    """按北京时间命名并保存 WAV，文件关闭后返回路径。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # 使用接收保存时的北京时间，与 WSL 系统时区无关。
    saved_at = datetime.now(timezone(timedelta(hours=8)))
    timestamp = saved_at.strftime("%Y%m%d_%H%M%S_%f")
    filename = f"audio_{timestamp}_{uuid4().hex}.wav"
    file_path = output_dir / filename

    with wave.open(str(file_path), "wb") as wav:

        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)

        wav.writeframes(pcm)

    return file_path
