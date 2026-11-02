"""进程组清理回归：覆盖 ros2 包装进程已退出但节点仍存活的情况。"""

import contextlib
import io
import signal
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from robot_visualization.runtime.supervisor import BackendSupervisor


class ProcessCleanupTests(unittest.TestCase):
    def test_exited_wrapper_does_not_hide_surviving_group(self):
        process = Mock(pid=43210)
        process.poll.return_value = 0
        with patch(
            "robot_visualization.runtime.supervisor.os.killpg", create=True
        ) as killpg:
            self.assertTrue(BackendSupervisor.group_is_alive(process))
        killpg.assert_called_once_with(43210, 0)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "需要 POSIX 信号")
    def test_cleanup_signals_group_even_when_wrapper_has_exited(self):
        manager = BackendSupervisor(SimpleNamespace())
        process = Mock(pid=43210)
        process.poll.return_value = 0
        manager.children["asr_node"] = process
        alive = True

        def killpg(group, sig):
            nonlocal alive
            if sig == 0 and not alive:
                raise ProcessLookupError()
            if sig == signal.SIGINT:
                alive = False

        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch(
                "robot_visualization.runtime.supervisor.os.killpg",
                side_effect=killpg,
                create=True,
            ) as send,
        ):
            manager.close()
        self.assertIn(
            (43210, signal.SIGINT), [call.args for call in send.call_args_list]
        )


if __name__ == "__main__":
    unittest.main()
