"""去重缓存容量与近期冲突保护，不运行节点。"""

import unittest
from pathlib import Path
from types import SimpleNamespace

from ai_edge.audio.audio_job_queue import AudioJobQueue


def message(index):
    stamp = SimpleNamespace(sec=1, nanosec=0)
    return SimpleNamespace(
        request_id=f"request-{index}",
        session_id="session",
        segment_id=f"segment-{index}",
        input_context_id="context",
        sample_rate=16000,
        channels=1,
        encoding="pcm_s16le",
        stamp=stamp,
        capture_started_at=stamp,
        capture_ended_at=stamp,
    )


class AudioQueueTests(unittest.TestCase):
    def test_cache_rolls_over_without_permanently_refusing_new_audio(self):
        queue = AudioJobQueue()
        path = Path("fixture.wav")
        for index in range(1100):
            self.assertTrue(queue.enqueue(message(index), path, b"\x00\x00")[0])
            queue.popleft()
        self.assertEqual(len(queue.seen_requests), 1024)
        self.assertEqual(len(queue.seen_inputs), 1024)
        self.assertTrue(queue.enqueue(message(1099), path, b"\x00\x00")[0])
        self.assertEqual(len(queue), 0)
        result = queue.enqueue(message(1099), path, b"\x01\x00")
        self.assertFalse(result[0])
        self.assertEqual(result[1], "REQUEST_ID_CONFLICT")


if __name__ == "__main__":
    unittest.main()
