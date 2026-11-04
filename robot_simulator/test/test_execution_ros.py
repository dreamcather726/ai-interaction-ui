"""ROS message contract checks with mock publishers; no executor or hardware."""

import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from builtin_interfaces.msg import Time
    from robot_interfaces.msg import TaskSnapshot
    from robot_simulator.execution import SimulationExecution
    from robot_simulator.execute_task_action_server import ExecuteTaskSimulator
    from robot_simulator.robot_state_simulator_node import RobotStateSimulator

    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False


@unittest.skipUnless(ROS_AVAILABLE, "需要构建后的 ROS 2 消息，不启动节点")
class SimulationContractTests(unittest.TestCase):
    def test_feedback_exposes_the_same_message_as_its_snapshot(self):
        frame = SimulationExecution().frame()
        clock = Mock()
        clock.now.return_value.to_msg.return_value = Time(sec=100)
        server = SimpleNamespace(
            lock=threading.Lock(),
            sequence=0,
            get_clock=lambda: clock,
            world_frame=lambda stamp: frame,
            world_publisher=Mock(),
            update_snapshot=ExecuteTaskSimulator.update_snapshot,
            snapshots=Mock(),
            sim_snapshots=Mock(),
        )
        handle = SimpleNamespace(
            request=SimpleNamespace(
                request_id="request", task_id="task", dispatch_id="dispatch"
            ),
            publish_feedback=Mock(),
        )
        snapshot = TaskSnapshot()
        ExecuteTaskSimulator.publish(server, handle, snapshot, "正在导航，等待到站")
        feedback = handle.publish_feedback.call_args.args[0]
        self.assertEqual(feedback.message, "正在导航，等待到站")
        self.assertEqual(feedback.message, feedback.task.message)
        self.assertEqual(
            feedback.task.message, server.snapshots.publish.call_args.args[0].message
        )

    def test_only_new_complete_camera_frames_refresh_camera_health(self):
        state = SimpleNamespace(last_camera=None, camera_stamp=0)
        message = SimpleNamespace(
            header=SimpleNamespace(stamp=Time(sec=100)),
            width=2,
            height=2,
            step=6,
            data=bytes(12),
        )
        with patch(
            "robot_simulator.robot_state_simulator_node.time.monotonic",
            return_value=10.0,
        ):
            RobotStateSimulator.on_camera(state, message)
        self.assertEqual(state.last_camera, 10.0)
        with patch(
            "robot_simulator.robot_state_simulator_node.time.monotonic",
            return_value=13.0,
        ):
            RobotStateSimulator.on_camera(state, message)
            self.assertEqual(state.last_camera, 10.0)
            message.header.stamp = Time(sec=101)
            message.data = bytes(1)
            RobotStateSimulator.on_camera(state, message)
            self.assertEqual(state.last_camera, 10.0)
            message.data = bytes(12)
            RobotStateSimulator.on_camera(state, message)
        self.assertEqual(state.last_camera, 13.0)


if __name__ == "__main__":
    unittest.main()
