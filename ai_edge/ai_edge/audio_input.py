"""校验共享目录中的 WAV 文件及其来源信息，不创建 ROS 服务。"""

import wave
from pathlib import Path


def read_audio_handoff(request, audio_root):
    """检查共享目录、WAV 格式及来源元数据，返回文件路径和 PCM。"""
    if not all(
        value.strip()
        for value in (
            request.request_id,
            request.session_id,
            request.segment_id,
            request.input_context_id,
        )
    ):
        raise ValueError("音频来源 ID 不能为空")
    path = Path(request.audio_path)
    if not path.is_absolute():
        raise ValueError("音频文件必须使用绝对路径")
    path = path.resolve(strict=True)
    if not path.is_relative_to(audio_root):
        raise ValueError("音频文件不在允许访问的目录内")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("WAV 文件超过 2 MiB 限制")
    if (request.sample_rate, request.channels, request.encoding) != (
        16000,
        1,
        "pcm_s16le",
    ):
        raise ValueError("音频采样参数不符合要求")
    times = (request.capture_started_at, request.capture_ended_at, request.stamp)
    if any(t.sec < 0 or not 0 <= t.nanosec < 10**9 for t in times):
        raise ValueError("录音时间戳格式错误")
    start, end, sent = (t.sec * 10**9 + t.nanosec for t in times)
    if not 0 < start < end <= sent or end - start > 10**10:
        raise ValueError("录音时间顺序错误或时长超过十秒")
    with wave.open(str(path), "rb") as wav:
        if (
            wav.getframerate(),
            wav.getnchannels(),
            wav.getsampwidth(),
            wav.getcomptype(),
        ) != (16000, 1, 2, "NONE"):
            raise ValueError("WAV 文件必须为 16 kHz 单声道 PCM16 格式")
        frames = wav.getnframes()
        if not 0 < frames <= 160000:
            raise ValueError("WAV 音频时长必须大于零且不超过十秒")
        pcm = wav.readframes(frames)
        if len(pcm) != frames * 2 or abs((end - start) / 10**9 - frames / 16000) > 0.05:
            raise ValueError("WAV 音频帧、数据长度或录音时间戳不一致")
    return path, pcm
