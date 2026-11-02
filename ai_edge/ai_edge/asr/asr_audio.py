"""校验 ASR 文件请求，复用 AI 端已有的共享目录和 WAV 格式约束。"""

from types import SimpleNamespace

from ai_edge.audio.audio_input import read_audio_handoff


def read_asr_request(request, audio_root):
    """ASR 请求不重复声明采样格式，因此以协议固定格式进行文件核验。"""
    metadata = {
        field: getattr(request, field)
        for field in (
            "stamp",
            "request_id",
            "session_id",
            "segment_id",
            "input_context_id",
            "capture_started_at",
            "capture_ended_at",
            "audio_path",
        )
    }
    metadata.update(sample_rate=16000, channels=1, encoding="pcm_s16le")
    return read_audio_handoff(SimpleNamespace(**metadata), audio_root)
