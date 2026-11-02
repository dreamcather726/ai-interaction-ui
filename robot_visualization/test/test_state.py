"""界面关联与进程命令规则回归，不依赖图形环境或硬件。"""

import contextlib
import io
import queue
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from robot_visualization.runtime.supervisor import BackendSupervisor, ros_command
from robot_visualization.state import RecordStore


def state(request="r1", sequence=1, boot="boot-a", stamp=100):
    return dict(
        request_id=request,
        source_node="/audio_processing_node",
        boot_id=boot,
        stamp_ns=stamp,
        event_seq=sequence,
        stage=0,
    )


class StoreTests(unittest.TestCase):
    def test_duplicate_and_old_sequence(self):
        store = RecordStore()
        self.assertTrue(store.apply("state", state(sequence=3)))
        self.assertFalse(store.apply("state", state(sequence=2)))
        self.assertFalse(store.apply("state", state(sequence=3)))

    def test_new_boot_rejects_late_old_process(self):
        store = RecordStore()
        store.apply("state", state())
        store.apply("asr", {"request_id": "r1", "text": "旧结果", "stamp_ns": 101})
        self.assertTrue(store.apply("state", state(boot="boot-b", stamp=200)))
        self.assertNotIn("asr", store.records["r1"])
        self.assertFalse(store.apply("state", state(sequence=99, stamp=300)))

    def test_unknown_old_boot_is_rejected(self):
        store = RecordStore()
        store.apply("state", state(boot="new", stamp=200))
        self.assertFalse(store.apply("state", state(boot="old", stamp=100)))

    def test_results_do_not_cross_request_ids(self):
        store = RecordStore()
        store.apply("state", state("one"))
        store.apply("asr", {"request_id": "two", "text": "第二段录音"})
        self.assertNotIn("asr", store.records["one"])

    def test_query_does_not_overwrite_newer_result(self):
        store = RecordStore()
        store.apply("asr", {"request_id": "r1", "text": "新结果", "stamp_ns": 5})
        self.assertFalse(
            store.apply("asr", {"request_id": "r1", "text": "旧结果", "stamp_ns": 3})
        )

    def test_cache_is_bounded(self):
        store = RecordStore(limit=2)
        for request in ("a", "b", "c"):
            store.apply("state", state(request))
        self.assertEqual(list(store.records), ["b", "c"])


class SupervisorRulesTests(unittest.TestCase):
    def test_child_output_reaches_console_before_process_exits(self):
        manager = BackendSupervisor(SimpleNamespace())
        output = io.StringIO()
        manager.log_file = io.StringIO()
        with contextlib.redirect_stdout(output):
            manager.spawn(
                "probe",
                [
                    sys.executable,
                    "-c",
                    "import time; print('UNBUFFERED_CHILD_READY'); time.sleep(30)",
                ],
            )
            process = manager.children["probe"]
            try:
                deadline = time.monotonic() + 5
                received = False
                while time.monotonic() < deadline:
                    try:
                        line = manager.lines.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if "[probe] UNBUFFERED_CHILD_READY" in line:
                        received = True
                        break
                self.assertTrue(received, "子进程首条输出被缓冲，未能实时送达")
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                process.wait(timeout=5)
                for reader in manager.readers:
                    reader.join(timeout=5)
        marker = "[probe] UNBUFFERED_CHILD_READY"
        self.assertEqual(output.getvalue().count(marker), 1)
        self.assertIn(marker, manager.log_file.getvalue())

    def test_commands_keep_arguments_out_of_shell(self):
        args = ros_command(
            "ai_edge",
            "asr_node",
            {"audio_root": "/tmp/音频 文件"},
            "/tmp/python env/bin/python",
        )
        self.assertEqual(args[:2], ["ros2", "run"])
        self.assertIn('audio_root:="/tmp/音频 文件"', args)
        self.assertEqual(args[3], "'/tmp/python env/bin/python'")

    def test_external_microphone_and_empty_whitelist(self):
        config = SimpleNamespace(
            audio_root=Path("/tmp/audio"),
            model_path="model",
            asr_python="/usr/bin/python3",
            model="qwen3.5:2b",
            ollama_url="http://127.0.0.1:11434",
            stations=[],
            objects=[],
            start_microphone=False,
        )
        plans = dict(BackendSupervisor(config).plans())
        self.assertNotIn("microphone_sender_node", plans)
        args = plans["audio_processing_node"]
        self.assertFalse(any("allowed_station_ids" in item for item in args))


if __name__ == "__main__":
    unittest.main()
