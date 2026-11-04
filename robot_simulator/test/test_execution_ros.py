"""ROS message contract checks with mock publishers; no executor or hardware."""

import threading
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from builtin_interfaces.msg import Time
    from rclpy.action import GoalResponse
    from ai_edge.common.catalog import CatalogStore
    from robot_interfaces.msg import DispatchReport, TaskSnapshot
    from robot_interfaces.srv import PrepareTask, QueryTask
    from robot_simulator.execution import SimulationExecution
    from robot_simulator.execute_task_action_server import ExecuteTaskSimulator
    from robot_simulator.robot_state_simulator_node import RobotStateSimulator

    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False


@unittest.skipUnless(ROS_AVAILABLE, "需要构建后的 ROS 2 消息，不启动节点")
class SimulationContractTests(unittest.TestCase):
    def test_prepare_is_pure_idempotent_and_revision_is_not_fixed(self):
        clock = Mock()
        clock.now.return_value.nanoseconds = 100 * 10**9
        clock.now.return_value.to_msg.return_value = Time(sec=100)
        server = SimpleNamespace(
            lock=threading.Lock(),
            task_records={},
            dispatch_records={},
            active_dispatch="",
            catalog_error="",
            reserved=None,
            prepared={},
            revision_counter=0,
            world=SimulationExecution(),
            get_clock=lambda: clock,
            refresh_catalog=Mock(),
        )
        request = PrepareTask.Request()
        request.request_id, request.confirmation_context_id = "prepare", "confirmation"
        request.task.task_id = "task"
        request.task.source_session_id = "session"
        request.task.source_input_id = "draft:one:v1"
        request.task.source_context_id = "context"
        request.task.candidate.task_type = 1
        request.task.candidate.destination_station_id = "station_a"
        first = ExecuteTaskSimulator.prepare(server, request, PrepareTask.Response())
        again = ExecuteTaskSimulator.prepare(server, request, PrepareTask.Response())
        self.assertTrue(first.success)
        self.assertEqual(first.revision, again.revision)
        self.assertEqual(server.world.state, "IDLE")
        self.assertEqual((server.world.x, server.world.y), (0.0, 0.0))
        request.request_id, request.task.task_id = "new-prepare", "another-task"
        second = ExecuteTaskSimulator.prepare(server, request, PrepareTask.Response())
        self.assertGreater(second.revision, first.revision)

    def test_query_returns_original_task_and_dispatch_or_explicit_not_found(self):
        snapshot, dispatch = TaskSnapshot(), DispatchReport()
        snapshot.task.task_id, snapshot.revision = "task", 7
        dispatch.task_id, dispatch.dispatch_id = "task", "dispatch"
        clock = Mock()
        clock.now.return_value.to_msg.return_value = Time(sec=100)
        server = SimpleNamespace(
            lock=threading.Lock(),
            task_records={"task": snapshot},
            dispatch_records={"dispatch": dispatch},
            active_task_id="",
            active_dispatch="",
            get_clock=lambda: clock,
        )
        request = QueryTask.Request()
        request.request_id, request.task_id, request.dispatch_id = (
            "query",
            "task",
            "dispatch",
        )
        found = ExecuteTaskSimulator.query(server, request, QueryTask.Response())
        self.assertTrue(found.success)
        self.assertEqual(found.task.revision, 7)
        request.dispatch_id = "missing"
        missing = ExecuteTaskSimulator.query(server, request, QueryTask.Response())
        self.assertFalse(missing.success)
        self.assertFalse(missing.dispatch_found)
        self.assertEqual(missing.error_code, "TASK_NOT_FOUND")

    def test_duplicate_dispatch_is_rejected_without_overwriting_original(self):
        report = DispatchReport()
        server = SimpleNamespace(
            lock=threading.Lock(),
            dispatch_records={"same": report},
            get_logger=lambda: Mock(),
        )
        result = ExecuteTaskSimulator.accept(
            server, SimpleNamespace(dispatch_id="same")
        )
        self.assertEqual(result, GoalResponse.REJECT)
        self.assertIs(server.dispatch_records["same"], report)

    def test_invalid_scene_update_cannot_execute_a_previously_prepared_task(self):
        server = SimpleNamespace(
            lock=threading.Lock(),
            refresh_catalog=Mock(),
            prepared={"task": object()},
            dispatch_records={},
            catalog_error="occupied station removed",
            get_clock=lambda: SimpleNamespace(
                now=lambda: SimpleNamespace(nanoseconds=100)
            ),
            get_logger=lambda: Mock(),
            world=Mock(),
        )
        response = ExecuteTaskSimulator.accept(
            server, SimpleNamespace(task_id="task", dispatch_id="dispatch")
        )
        self.assertEqual(response, GoalResponse.REJECT)
        server.world.start.assert_not_called()

    def test_execution_gateway_reads_the_same_catalog_and_invalidates_old_preflight(
        self,
    ):
        with tempfile.TemporaryDirectory() as folder:
            catalog = CatalogStore(Path(folder) / "catalog.sqlite3")
            snapshot = catalog.initialize(["1", "2"], ["x"])
            server = SimpleNamespace(
                catalog=catalog,
                active_dispatch="",
                last_catalog_check=0.0,
                catalog_revision=None,
                catalog_initialized=False,
                catalog_error="",
                world=SimulationExecution(),
                station_names={},
                prepared={"old": object()},
                get_logger=lambda: Mock(),
            )
            ExecuteTaskSimulator.refresh_catalog(server, force=True)
            self.assertEqual(set(server.world.stations), {"1", "2"})
            self.assertEqual(server.world.objects, {"x": "1"})
            self.assertFalse(server.prepared)
            location = server.world.stations["2"]
            updated = catalog.update(
                "save",
                "stations",
                dict(id="3", name="三号点", aliases=[], enabled=True),
                snapshot["revision"],
            )
            server.prepared["another"] = object()
            server.active_dispatch = "running"
            ExecuteTaskSimulator.refresh_catalog(server, force=True)
            self.assertNotIn("3", server.world.stations)
            server.active_dispatch = ""
            ExecuteTaskSimulator.refresh_catalog(server, force=True)
            self.assertIn("3", server.world.stations)
            self.assertEqual(server.world.stations["2"], location)
            self.assertEqual(server.catalog_revision, updated["revision"])
            self.assertFalse(server.prepared)

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
            task_records={},
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
