"""离屏 Qt 交互测试；模拟的展示数据只用于测试，不进入生产界面。"""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from robot_visualization.ui.window import DashboardWindow

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False


@unittest.skipUnless(QT_AVAILABLE, "需要 PySide6")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = DashboardWindow(
            SimpleNamespace(
                model="qwen3.5:2b",
                stations=[],
                objects=[],
                audio_root=Path("/tmp/audio"),
            )
        )
        self.window.show()
        self.app.processEvents()
        self.value = dict(
            services={},
            start=True,
            stop=True,
            asr_ready=True,
            recording=False,
            query=True,
            connected=True,
        )

    def tearDown(self):
        self.window.can_close = True
        self.window.close()

    def test_initial_screen_does_not_invent_results(self):
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertEqual(self.window.asr_text.toPlainText(), "")
        self.assertEqual(self.window.history.count(), 0)

    def test_duplicate_click_does_not_send_two_starts(self):
        sent = []
        self.window.start_requested.connect(lambda: sent.append("start"))
        self.window.receive_availability(self.value)
        QTest.mouseClick(self.window.start_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.window.start_button, Qt.MouseButton.LeftButton)
        self.assertEqual(sent, ["start"])

    def test_timer_cannot_assume_capture_stopped(self):
        self.window.receive_availability(dict(self.value, recording=True))
        self.window.recording_since -= 12
        self.window.tick()
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())

    def test_missing_device_state_never_enables_start(self):
        self.window.receive_availability(dict(self.value, recording=None))
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
