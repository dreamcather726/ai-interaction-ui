"""
音频文件交接校验模块

功能：
1. 检查音频请求的来源信息
2. 检查 WAV 文件路径和文件大小
3. 检查音频采样参数
4. 检查录音时间戳
5. 检查 WAV 文件格式
6. 读取 WAV 中的 PCM 音频数据

注意：本文件只负责音频数据校验，不创建 ROS2 Service。
"""

import wave
from pathlib import Path


def read_audio_handoff(request, audio_root):
    """
    校验音频文件及其来源信息。

    参数：
        request：
            音频请求对象，包含请求ID、文件路径、
            采样率、声道、录音时间等信息。

        audio_root：
            允许访问的音频文件根目录。

    返回：
        path：通过校验的 WAV 文件路径
        pcm：从 WAV 文件中读取的原始 PCM 数据

    异常：
        ValueError：音频信息或文件不符合要求
    """

    # ==================================================
    # 1. 检查音频请求的来源标识
    # ==================================================

    # 这些 ID 用于区分不同的请求、会话和音频片段
    identifiers = (
        request.request_id,
        request.session_id,
        request.segment_id,
        request.input_context_id,
    )

    # 检查所有 ID 是否为空
    if not all(value.strip() for value in identifiers):
        raise ValueError("音频来源 ID 不能为空")

    # ==================================================
    # 2. 检查 WAV 文件路径
    # ==================================================

    # 将传入的文件路径转换为 Path 对象
    path = Path(request.audio_path)

    # 必须使用绝对路径，避免相对路径导致文件位置不明确
    if not path.is_absolute():
        raise ValueError("音频文件必须使用绝对路径")

    # 解析真实文件路径，并确认文件确实存在
    path = path.resolve(strict=True)

    # 限制文件只能位于指定的音频目录内
    # 防止读取音频根目录以外的其他文件
    if not path.is_relative_to(audio_root):
        raise ValueError("音频文件不在允许访问的目录内")

    # ==================================================
    # 3. 检查 WAV 文件大小
    # ==================================================

    # 最长 300 秒 PCM16 的 WAV 文件小于 10 MiB。
    # 防止异常文件占用过多存储空间
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("WAV 文件超过 10 MiB 限制")

    # ==================================================
    # 4. 检查音频基本参数
    # ==================================================

    # 规定系统当前只接受：
    # 采样率：16000 Hz
    # 声道数：1（单声道）
    # 编码格式：PCM 16bit 小端序
    expected_metadata = (
        16000,
        1,
        "pcm_s16le",
    )

    # 获取请求中携带的实际音频参数
    actual_metadata = (
        request.sample_rate,
        request.channels,
        request.encoding,
    )

    # 对比实际参数与系统规定参数
    if actual_metadata != expected_metadata:
        raise ValueError("音频采样参数不符合要求")

    # ==================================================
    # 5. 检查录音时间戳
    # ==================================================

    # 获取录音开始时间、结束时间和发送时间
    timestamps = (
        request.capture_started_at,
        request.capture_ended_at,
        request.stamp,
    )

    # 检查 ROS2 时间戳是否合法
    for timestamp in timestamps:

        # nanosec 必须处于 0 到 999999999 之间
        if timestamp.sec < 0 or not 0 <= timestamp.nanosec < 10**9:
            raise ValueError("录音时间戳格式错误")

    # 将 ROS2 时间戳转换为纳秒，方便后续计算
    start, end, sent = (
        timestamp.sec * 10**9 + timestamp.nanosec for timestamp in timestamps
    )

    # 检查时间顺序：
    # 开始时间 < 结束时间 <= 发送时间
    if not 0 < start < end <= sent:
        raise ValueError("录音时间顺序错误")

    # 录音长度与音频传输接口保持一致。
    if end - start > 300 * 10**9:
        raise ValueError("录音时长超过 300 秒")

    # ==================================================
    # 6. 打开 WAV 文件并检查文件格式
    # ==================================================

    # 使用 wave 模块读取 WAV 文件
    with wave.open(str(path), "rb") as wav:

        # 获取 WAV 文件的实际参数
        wav_metadata = (
            wav.getframerate(),  # 采样率
            wav.getnchannels(),  # 声道数
            wav.getsampwidth(),  # 每个采样点的字节数
            wav.getcomptype(),  # 音频压缩类型
        )

        # WAV 文件必须满足以下条件：
        # 16000 Hz、单声道、16bit、无压缩 PCM
        expected_wav_metadata = (
            16000,
            1,
            2,
            "NONE",
        )

        # 检查 WAV 文件实际格式
        if wav_metadata != expected_wav_metadata:
            raise ValueError("WAV 文件必须为 16kHz 单声道 PCM16 格式")

        # ==================================================
        # 7. 检查音频帧数量
        # ==================================================

        # 获取 WAV 文件包含的音频帧总数
        frames = wav.getnframes()

        # 16000 帧对应 1 秒音频
        # 最大 4800000 帧对应 300 秒音频。
        if not 0 < frames <= 4800000:
            raise ValueError("WAV 音频时长必须在 0 到 300 秒之间")

        # ==================================================
        # 8. 读取 WAV 中的 PCM 原始数据
        # ==================================================

        # 读取全部音频帧，得到 PCM 字节数据
        pcm = wav.readframes(frames)

        # 单声道 16bit 音频：
        # 每个采样点占用 2 字节
        # 所以 PCM 总字节数应该等于 frames * 2
        if len(pcm) != frames * 2:
            raise ValueError("WAV 音频帧数量或数据长度不匹配")

        # ==================================================
        # 9. 校验录音实际时长
        # ==================================================

        # 根据音频帧数计算实际音频时长
        actual_duration = frames / 16000

        # 根据录音开始和结束时间计算录音时长
        capture_duration = (end - start) / 10**9

        # 两种计算方式允许存在最多 50ms 的误差
        if abs(capture_duration - actual_duration) > 0.05:
            raise ValueError("WAV 文件时长与录音时间戳不匹配")

    # ==================================================
    # 10. 返回校验完成的音频数据
    # ==================================================

    # path：WAV 文件路径
    # pcm：WAV 文件内部的原始 PCM 音频数据
    return path, pcm
