"""验证不完整权重不能让 ASR 错误就绪；不下载模型或使用真实麦克风。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from ai_edge.asr.funasr_backend import ASRError, FunASRBackend


class ModelLoadingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.funasr = ModuleType("funasr")
        self.funasr.AutoModel = Mock()
        patcher = patch.dict("sys.modules", {"funasr": self.funasr})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_partial_download_never_constructs_random_model(self):
        (self.root / "model.pt.incomplete").write_bytes(b"partial")
        backend = FunASRBackend(str(self.root))
        with self.assertRaises(ASRError) as caught:
            backend.load()
        self.assertEqual(caught.exception.code, "ASR_MODEL_INCOMPLETE")
        self.assertIn(".incomplete", str(caught.exception))
        self.funasr.AutoModel.assert_not_called()
        self.assertIsNone(backend.model)

    def test_missing_local_directory_does_not_trigger_hub_download(self):
        backend = FunASRBackend(str(self.root / "missing"))
        with self.assertRaises(ASRError) as caught:
            backend.load()
        self.assertEqual(caught.exception.code, "ASR_MODEL_INCOMPLETE")
        self.funasr.AutoModel.assert_not_called()

    def test_manifest_checkpoint_is_passed_to_loader_explicitly(self):
        checkpoint = self.root / "custom.pt"
        checkpoint.write_bytes(b"fixture")
        (self.root / "configuration.json").write_text(
            json.dumps(
                {
                    "file_path_metas": {"init_param": "custom.pt"},
                }
            ),
            encoding="utf-8",
        )
        backend = FunASRBackend(str(self.root))
        backend.load()
        options = self.funasr.AutoModel.call_args.kwargs
        self.assertEqual(options["init_param"], str(checkpoint))
        self.assertFalse(options["ignore_init_mismatch"])
        self.assertIs(backend.model, self.funasr.AutoModel.return_value)

    def test_hub_model_without_checkpoint_never_becomes_ready(self):
        self.funasr.AutoModel.return_value = SimpleNamespace(kwargs={})
        backend = FunASRBackend("iic/SenseVoiceSmall")
        with self.assertRaises(ASRError) as caught:
            backend.load()
        self.assertEqual(caught.exception.code, "ASR_MODEL_INCOMPLETE")
        self.assertIsNone(backend.model)

    def test_checkpoint_load_failure_preserves_unready_state(self):
        (self.root / "model.pt").write_bytes(b"truncated")
        self.funasr.AutoModel.side_effect = RuntimeError("invalid checkpoint")
        backend = FunASRBackend(str(self.root))
        with self.assertRaises(ASRError) as caught:
            backend.load()
        self.assertEqual(caught.exception.code, "ASR_MODEL_UNAVAILABLE")
        self.assertIsNone(backend.model)


if __name__ == "__main__":
    unittest.main()
