"""模型路径应随项目定位，不能跟随无关的工作目录或个人缓存。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_edge.asr.model_paths import default_model_path


class ModelPathTests(unittest.TestCase):
    def test_active_project_takes_priority_over_another_install(self):
        with tempfile.TemporaryDirectory() as folder:
            installed = Path(folder) / "installed-project"
            active = Path(folder) / "active-project"
            for root in (installed, active):
                for package in ("ai_edge", "robot_visualization"):
                    (root / package).mkdir(parents=True)
                    (root / package / "package.xml").touch()
            entry = (
                installed
                / "install/ai_edge/lib/python3.10/site-packages/ai_edge/asr/model_paths.py"
            )
            with (
                patch("ai_edge.asr.model_paths.__file__", str(entry)),
                patch("ai_edge.asr.model_paths.Path.cwd", return_value=active),
            ):
                self.assertEqual(
                    default_model_path(), active / "models/SenseVoiceSmall"
                )

    def test_installed_entry_finds_models_in_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            for package in ("ai_edge", "robot_visualization"):
                (root / package).mkdir(parents=True)
                (root / package / "package.xml").touch()
            entry = (
                root
                / "install/ai_edge/lib/python3.10/site-packages/ai_edge/asr/model_paths.py"
            )
            with (
                patch("ai_edge.asr.model_paths.__file__", str(entry)),
                patch("ai_edge.asr.model_paths.Path.cwd", return_value=Path(folder)),
            ):
                self.assertEqual(default_model_path(), root / "models/SenseVoiceSmall")

    def test_workspace_install_finds_repository_under_src(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "workspace"
            root = workspace / "src"
            for package in ("ai_edge", "robot_visualization"):
                (root / package).mkdir(parents=True)
                (root / package / "package.xml").touch()
            entry = (
                workspace
                / "install/ai_edge/lib/python3.10/site-packages/ai_edge/asr/model_paths.py"
            )
            with (
                patch("ai_edge.asr.model_paths.__file__", str(entry)),
                patch("ai_edge.asr.model_paths.Path.cwd", return_value=Path(folder)),
            ):
                self.assertEqual(default_model_path(), root / "models/SenseVoiceSmall")


if __name__ == "__main__":
    unittest.main()
