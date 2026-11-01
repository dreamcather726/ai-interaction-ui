"""Validate a shared WAV path and its source metadata; no ROS service."""

from pathlib import Path
import wave


def read_audio_handoff(request, audio_root):
    if not all(value.strip() for value in (
            request.request_id, request.session_id, request.segment_id, request.input_context_id)):
        raise ValueError('Source IDs must be nonempty')
    path = Path(request.audio_path)
    if not path.is_absolute():
        raise ValueError('audio_path must be absolute')
    path = path.resolve(strict=True)
    if not path.is_relative_to(audio_root):
        raise ValueError('Audio file is outside configured audio_root')
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('WAV file exceeds 2 MiB limit')
    if (request.sample_rate, request.channels, request.encoding) != (16000, 1, 'pcm_s16le'):
        raise ValueError('Unsupported audio metadata')
    times = (request.capture_started_at, request.capture_ended_at, request.stamp)
    if any(t.sec < 0 or not 0 <= t.nanosec < 10**9 for t in times):
        raise ValueError('Invalid timestamp')
    start, end, sent = (t.sec * 10**9 + t.nanosec for t in times)
    if not 0 < start < end <= sent or end - start > 10**10:
        raise ValueError('Invalid capture time order or duration')
    with wave.open(str(path), 'rb') as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (16000, 1, 2, 'NONE'):
            raise ValueError('Expected 16 kHz mono PCM16 WAV')
        frames = wav.getnframes()
        if not 0 < frames <= 160000:
            raise ValueError('WAV duration must be in (0, 10] seconds')
        pcm = wav.readframes(frames)
        if len(pcm) != frames * 2 or abs((end - start) / 10**9 - frames / 16000) > 0.05:
            raise ValueError('WAV frame count, data or capture duration mismatch')
    return path, pcm
