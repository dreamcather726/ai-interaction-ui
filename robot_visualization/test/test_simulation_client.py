"""Client protocol regressions using controlled futures/time; no nodes or hardware."""

from concurrent.futures import Future
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from builtin_interfaces.msg import Time
    from robot_interfaces.action import ExecuteTask
    from robot_interfaces.msg import NodeHeartbeat, RobotStatus
    from robot_interfaces.srv import PrepareTask, QueryTask
    from robot_visualization.connection import RobotConnection
    from robot_visualization.ros.simulation import SimulationClient

    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False


@unittest.skipUnless(ROS_AVAILABLE, "需要构建后的 ROS 2 接口，不启动节点")
class SimulationClientTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        clock = patch(
            "robot_visualization.ros.robot.time.monotonic",
            side_effect=lambda: self.now,
        )
        clock.start()
        self.addCleanup(clock.stop)
        self.action = Mock()
        action_type = patch(
            "robot_visualization.ros.robot.ActionClient", return_value=self.action
        )
        self.action_factory = action_type.start()
        self.addCleanup(action_type.stop)
        self.node, self.events = Mock(), Mock()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.node.config = SimpleNamespace(
            robot=replace(
                RobotConnection(), journal_path=str(Path(folder.name) / "dispatch.json")
            )
        )
        telemetry = patch("robot_visualization.ros.telemetry.RobotTelemetry")
        telemetry.start()
        self.addCleanup(telemetry.stop)
        graph = patch.object(SimulationClient, "provider_counts", return_value=[1, 1])
        graph.start()
        self.addCleanup(graph.stop)
        self.query = Mock()
        self.query.service_is_ready.return_value = False
        self.prepare = Mock()
        self.cancel_service = Mock()
        self.node.create_client.side_effect = lambda kind, name: (
            self.prepare
            if kind is PrepareTask
            else self.query if kind is QueryTask else self.cancel_service
        )
        self.node.get_clock.return_value.now.return_value.to_msg.return_value = Time(
            sec=100
        )
        self.node.get_clock.return_value.now.return_value.nanoseconds = 100 * 10**9
        self.prepare.service_is_ready.return_value = True
        self.action.server_is_ready.return_value = True
        self.client = SimulationClient(self.node, self.events)
        self.heartbeat()
        self.draft = dict(
            draft_id="draft-one",
            revision=1,
            state=1,
            session_id="session-one",
            input_context_id="context-one",
            candidate=dict(task_type="NAVIGATE", destination_station_id="station_a"),
        )

    def last_task(self):
        return next(
            call.args[1]
            for call in reversed(self.events.record.emit.call_args_list)
            if call.args[0] == "robot_task"
        )

    def heartbeat(self, boot="execution-boot"):
        status = RobotStatus()
        status.stamp = Time(sec=int(self.now))
        status.source_node = self.node.config.robot.executor_node
        status.state = status.EXECUTING if self.client.goal else status.IDLE
        if self.client.goal:
            status.active_task_id, status.active_dispatch_id = (
                self.client.goal.task_id,
                self.client.goal.dispatch_id,
            )
        self.client.on_status(status)
        message = NodeHeartbeat()
        message.node_name, message.boot_id = status.source_node, boot
        message.sequence = self.client.heartbeat_sequence + 1
        message.state = message.RUNNING
        self.client.on_heartbeat(message)

    def start_prepare(self):
        self.prepare_future = Future()
        self.prepare.call_async.return_value = self.prepare_future
        self.client.prepare(self.draft)
        return self.prepare.call_async.call_args.args[0]

    def accept_prepare(self):
        request = self.start_prepare()
        response = PrepareTask.Response()
        response.success = True
        response.request_id = request.request_id
        response.task = request.task
        response.revision = 1
        response.confirmation_context_id = request.confirmation_context_id
        response.confirmation_expires_at = Time(sec=400)
        self.goal_future = Future()
        self.action.send_goal_async.return_value = self.goal_future
        self.prepare_future.set_result(response)
        self.client.poll()
        self.action.send_goal_async.assert_not_called()
        self.client.execute(self.draft)

    def start_execution(self):
        self.accept_prepare()
        self.result_future, self.cancel_future = Future(), Future()
        self.handle = Mock(accepted=True)
        self.handle.goal_id.uuid = list(range(16))
        self.handle.get_result_async.return_value = self.result_future
        self.handle.cancel_goal_async.return_value = self.cancel_future
        self.goal_future.set_result(self.handle)
        self.client.poll()

    def feedback(self, sequence=1, task_id=None):
        self.heartbeat()
        feedback = ExecuteTask.Feedback()
        feedback.request_id = self.client.goal.request_id
        feedback.task_id = task_id or self.client.goal.task_id
        feedback.dispatch_id = self.client.goal.dispatch_id
        feedback.task.task = deepcopy(self.client.prepare_request.task)
        feedback.task.revision = self.client.goal.revision
        feedback.task.active_dispatch_id = self.client.goal.dispatch_id
        feedback.task.event_seq = sequence
        feedback.task.state = feedback.task.RUNNING
        feedback.task.step = "1/4 导航到 station_a"
        feedback.task.message = "正在导航，等待到站"
        feedback.task.progress = 0.25
        self.client.on_feedback(SimpleNamespace(feedback=feedback))

    def result(self, canceled=False):
        result = ExecuteTask.Result()
        goal = self.client.goal
        result.request_id, result.task_id, result.dispatch_id = (
            goal.request_id,
            goal.task_id,
            goal.dispatch_id,
        )
        result.task.task = deepcopy(self.client.prepare_request.task)
        result.task.revision = goal.revision
        result.task.active_dispatch_id = goal.dispatch_id
        result.success = not canceled
        result.task.state = result.task.CANCELED if canceled else result.task.SUCCEEDED
        result.task.progress = 0.25 if canceled else 1.0
        result.error_code = "CANCELED" if canceled else ""
        result.message = "模拟执行已取消" if canceled else "模拟执行完成"
        self.result_future.set_result(
            SimpleNamespace(result=result, status=5 if canceled else 4)
        )
        self.client.poll()

    def test_prepare_rejection_can_be_retried_manually_with_original_ids(self):
        request = self.start_prepare()
        response = PrepareTask.Response()
        response.request_id = request.request_id
        response.message, response.error_code = "物料尚未就绪", "OBJECT_NOT_AT_STATION"
        self.prepare_future.set_result(response)
        self.client.poll()
        self.assertTrue(self.last_task()["retryable"])
        self.assertFalse(self.client.dispatched)
        self.action.send_goal_async.assert_not_called()
        retried = self.start_prepare()
        self.assertEqual(retried.request_id, request.request_id)
        self.assertEqual(retried.task.task_id, request.task.task_id)
        self.assertEqual(
            retried.confirmation_context_id, request.confirmation_context_id
        )
        self.assertFalse(self.last_task()["retryable"])

    def test_prepare_timeout_never_sends_goal_and_allows_explicit_retry(self):
        request = self.start_prepare()
        self.now += 6
        self.client.poll()
        self.heartbeat()
        self.assertTrue(self.prepare_future.cancelled())
        self.assertTrue(self.last_task()["retryable"])
        self.assertEqual(self.last_task()["error_code"], "PREPARE_TIMEOUT")
        self.action.send_goal_async.assert_not_called()
        self.assertEqual(self.start_prepare().request_id, request.request_id)

    def test_long_execution_uses_feedback_liveness_not_total_duration(self):
        self.start_execution()
        for sequence in range(1, 31):
            self.now += 4
            self.feedback(sequence)
            self.client.poll()
            self.assertFalse(self.client.uncertain)
        self.assertTrue(self.last_task()["active"])
        self.assertEqual(self.last_task()["message"], "正在导航，等待到站")
        self.assertEqual(self.last_task()["progress"], 0.25)

    def test_lost_feedback_is_unknown_and_late_result_resolves_original_attempt(self):
        self.start_execution()
        self.feedback()
        dispatch_id = self.client.goal.dispatch_id
        self.now += 6
        self.client.poll()
        self.assertTrue(self.last_task()["execution_unknown"])
        self.assertFalse(self.last_task()["active"])
        self.assertIsNone(self.last_task()["state"])
        self.assertFalse(self.result_future.cancelled())
        self.client.execute(dict(self.draft, draft_id="another-draft"))
        self.assertEqual(self.client.goal.dispatch_id, dispatch_id)
        self.assertEqual(self.action.send_goal_async.call_count, 1)
        self.result()
        self.assertFalse(self.last_task()["execution_unknown"])
        self.assertEqual(self.last_task()["progress"], 1.0)

    def test_feedback_recovery_resumes_tracking_without_new_goal(self):
        self.start_execution()
        self.now += 6
        self.client.poll()
        self.feedback()
        self.assertFalse(self.client.uncertain)
        self.assertTrue(self.last_task()["active"])
        self.assertEqual(self.action.send_goal_async.call_count, 1)

    def test_old_or_unrelated_feedback_cannot_extend_liveness(self):
        self.start_execution()
        self.feedback(sequence=2)
        self.now += 4
        self.feedback(sequence=2)
        self.feedback(sequence=3, task_id="unrelated-task")
        self.now += 2
        self.client.poll()
        self.assertTrue(self.client.uncertain)

    def test_cancel_acceptance_does_not_claim_stopped_and_has_its_own_deadline(self):
        self.start_execution()
        self.client.cancel()
        self.client.cancel()
        self.handle.cancel_goal_async.assert_called_once()
        self.cancel_future.set_result(
            SimpleNamespace(
                goals_canceling=[SimpleNamespace(goal_id=self.handle.goal_id)]
            )
        )
        self.client.poll()
        self.assertTrue(self.last_task()["canceling"])
        self.assertTrue(self.last_task()["active"])
        self.now += 4
        self.feedback()
        self.now += 2
        self.client.poll()
        self.assertTrue(self.last_task()["execution_unknown"])
        self.assertEqual(self.last_task()["error_code"], "CANCEL_NOT_CONFIRMED")
        self.result(canceled=True)
        self.assertFalse(self.last_task()["execution_unknown"])
        self.assertFalse(self.last_task()["active"])
        self.assertEqual(self.last_task()["state"], 5)

    def test_cancel_rejection_is_unknown(self):
        self.start_execution()
        self.client.cancel()
        self.cancel_future.set_result(SimpleNamespace(goals_canceling=[]))
        self.client.poll()
        self.assertTrue(self.last_task()["execution_unknown"])
        self.assertFalse(self.last_task()["retryable"])
        self.assertEqual(self.last_task()["error_code"], "CANCEL_NOT_CONFIRMED")

    def test_missing_cancel_response_times_out_even_with_execution_feedback(self):
        self.start_execution()
        self.client.cancel()
        self.now += 4
        self.feedback()
        self.now += 2
        self.client.poll()
        self.assertFalse(self.cancel_future.done())
        self.assertTrue(self.last_task()["execution_unknown"])
        self.assertEqual(self.last_task()["error_code"], "CANCEL_NOT_CONFIRMED")
        self.feedback(sequence=2)
        self.assertTrue(self.last_task()["execution_unknown"])

    def test_failed_goal_send_is_unknown_and_not_eligible_for_prepare_retry(self):
        request = self.start_prepare()
        response = PrepareTask.Response()
        response.success = True
        response.request_id = request.request_id
        response.task = request.task
        response.revision = 1
        response.confirmation_context_id = request.confirmation_context_id
        response.confirmation_expires_at = Time(sec=400)
        self.action.send_goal_async.side_effect = RuntimeError("transport unavailable")
        self.prepare_future.set_result(response)
        self.client.poll()
        self.action.send_goal_async.assert_not_called()
        self.client.execute(self.draft)
        self.assertTrue(self.last_task()["execution_unknown"])
        self.assertFalse(self.last_task()["retryable"])
        self.client.execute(self.draft)
        self.assertEqual(self.action.send_goal_async.call_count, 1)

    def test_goal_response_timeout_cancels_late_acceptance_without_resending(self):
        self.accept_prepare()
        self.now += 6
        self.client.poll()
        self.assertTrue(self.last_task()["execution_unknown"])
        handle = Mock(accepted=True)
        handle.get_result_async.return_value = Future()
        handle.cancel_goal_async.return_value = Future()
        self.goal_future.set_result(handle)
        self.client.poll()
        handle.cancel_goal_async.assert_called_once()
        self.assertEqual(self.action.send_goal_async.call_count, 1)

    def test_mismatched_result_stays_unknown_and_tracks_defensive_cancel(self):
        self.start_execution()
        result = ExecuteTask.Result()
        result.request_id = "unrelated-request"
        self.result_future.set_result(SimpleNamespace(result=result))
        self.client.poll()
        self.assertTrue(self.last_task()["execution_unknown"])
        self.handle.cancel_goal_async.assert_called_once()
        self.assertIsNone(self.client.pending)
        self.now += 6
        self.client.poll()
        self.assertEqual(self.last_task()["error_code"], "CANCEL_NOT_CONFIRMED")

    def test_preflight_never_sends_goal_until_matching_review_is_confirmed(self):
        self.draft["state"] = 0
        request = self.start_prepare()
        response = PrepareTask.Response()
        response.success = True
        response.request_id = request.request_id
        response.task = request.task
        response.revision = 1
        response.confirmation_context_id = request.confirmation_context_id
        response.confirmation_expires_at = Time(sec=400)
        self.prepare_future.set_result(response)
        self.client.poll()
        self.assertTrue(self.last_task()["prepared"])
        self.assertFalse(self.last_task()["active"])
        self.action.send_goal_async.assert_not_called()
        self.assertEqual(self.client.prepared.task.task_id, request.task.task_id)
        self.draft["state"] = 1
        self.action.send_goal_async.return_value = Future()
        self.client.execute(self.draft)
        self.action.send_goal_async.assert_called_once()

    def test_execution_heartbeat_keeps_long_step_alive_without_progress_feedback(self):
        self.start_execution()
        for _sequence in range(1, 31):
            self.now += 4
            self.heartbeat()
            self.client.poll()
            self.assertFalse(self.client.uncertain)
        self.assertEqual(self.action.send_goal_async.call_count, 1)
        self.now += 6
        self.client.poll()
        self.assertTrue(self.last_task()["execution_unknown"])

    def test_changed_draft_cannot_use_an_old_preflight(self):
        self.draft["state"] = 0
        request = self.start_prepare()
        response = PrepareTask.Response()
        response.success = True
        response.request_id = request.request_id
        response.task = request.task
        response.revision = 1
        response.confirmation_context_id = request.confirmation_context_id
        response.confirmation_expires_at = Time(sec=400)
        self.prepare_future.set_result(response)
        self.client.poll()
        changed = dict(self.draft, state=1, revision=2)
        self.client.execute(changed)
        self.action.send_goal_async.assert_not_called()
        self.assertTrue(self.last_task()["retryable"])

    def test_endpoint_names_are_public_and_identical_in_both_modes(self):
        expected = ("/task/prepare", "/task/query", "/task/cancel")
        for name in expected:
            self.assertIn(
                name, [call.args[1] for call in self.node.create_client.call_args_list]
            )
        self.action_factory.assert_called_once_with(
            self.node, ExecuteTask, "/task/execute"
        )
        self.assertNotIn("simulator", self.client.config.execute_action)

    def test_non_unit_prepared_revision_is_forwarded_to_goal(self):
        request = self.start_prepare()
        response = PrepareTask.Response()
        response.success, response.request_id, response.task = (
            True,
            request.request_id,
            request.task,
        )
        response.revision = 17
        response.confirmation_context_id = request.confirmation_context_id
        response.confirmation_expires_at = Time(sec=400)
        self.prepare_future.set_result(response)
        self.client.poll()
        self.action.send_goal_async.return_value = Future()
        self.client.execute(self.draft)
        self.assertEqual(self.action.send_goal_async.call_args.args[0].revision, 17)

    def test_new_executor_boot_invalidates_preflight_and_blocks_dispatch(self):
        self.accept_prepare()
        self.heartbeat("different-boot")
        self.assertTrue(self.client.uncertain)
        self.client.execute(self.draft)
        self.assertEqual(self.action.send_goal_async.call_count, 1)

    def test_changed_prepared_content_is_shown_and_cannot_execute(self):
        request = self.start_prepare()
        response = PrepareTask.Response()
        response.success, response.request_id = True, request.request_id
        response.task = deepcopy(request.task)
        response.task.candidate.destination_station_id = "station_b"
        response.revision = 5
        response.confirmation_context_id = request.confirmation_context_id
        response.confirmation_expires_at = Time(sec=400)
        self.prepare_future.set_result(response)
        self.client.poll()
        self.assertIsNone(self.client.prepared)
        self.assertEqual(
            self.last_task()["revised_candidate"]["destination_station_id"], "station_b"
        )
        self.client.execute(self.draft)
        self.action.send_goal_async.assert_not_called()

    def test_missing_query_keeps_original_attempt_unknown_without_resend(self):
        self.start_execution()
        self.now += 6
        self.client.poll()
        self.query.service_is_ready.return_value = True
        future = Future()
        self.query.call_async.return_value = future
        self.heartbeat()
        self.client.query()
        request = self.query.call_async.call_args.args[0]
        response = QueryTask.Response()
        response.request_id = request.request_id
        future.set_result(response)
        self.client.poll()
        self.assertTrue(self.client.uncertain)
        self.assertEqual(self.action.send_goal_async.call_count, 1)

    def test_restart_loads_original_dispatch_and_only_queries(self):
        self.start_execution()
        goal = self.client.goal
        restored = SimulationClient(self.node, self.events)
        self.assertEqual(restored.goal.dispatch_id, goal.dispatch_id)
        self.assertTrue(restored.uncertain)
        restored.execute(self.draft)
        self.assertEqual(self.action.send_goal_async.call_count, 1)

    def test_matching_query_terminal_resolves_unknown_without_new_goal(self):
        self.start_execution()
        self.now += 6
        self.client.poll()
        self.query.service_is_ready.return_value = True
        future = Future()
        self.query.call_async.return_value = future
        self.heartbeat()
        self.client.query()
        response = QueryTask.Response()
        response.request_id = self.query.call_async.call_args.args[0].request_id
        response.success = response.task_found = response.dispatch_found = True
        response.task.task = deepcopy(self.client.prepare_request.task)
        response.task.revision = self.client.goal.revision
        response.task.active_dispatch_id = self.client.goal.dispatch_id
        response.task.state, response.task.progress = response.task.SUCCEEDED, 1.0
        for field in (
            "request_id",
            "task_id",
            "dispatch_id",
            "revision",
            "session_id",
            "input_context_id",
        ):
            setattr(response.dispatch, field, getattr(self.client.goal, field))
        response.dispatch.decision, response.dispatch.success = (
            response.dispatch.ACCEPTED,
            True,
        )
        future.set_result(response)
        self.client.poll()
        self.assertFalse(self.client.uncertain)
        self.assertEqual(self.last_task()["state"], 3)
        self.assertEqual(self.action.send_goal_async.call_count, 1)

    def test_multiple_executor_providers_block_preflight(self):
        with patch.object(self.client, "provider_counts", return_value=[2, 2]):
            self.client.prepare(self.draft)
        self.prepare.call_async.assert_not_called()
        self.action.send_goal_async.assert_not_called()

    def test_explicit_goal_rejection_is_retained_as_known_history_after_restart(self):
        self.accept_prepare()
        self.goal_future.set_result(SimpleNamespace(accepted=False))
        self.client.poll()
        self.assertFalse(self.client.uncertain)
        restored = SimulationClient(self.node, self.events)
        self.assertFalse(restored.uncertain)
        self.assertEqual(restored.terminal_snapshot.state, 4)
        self.assertEqual(restored.terminal_snapshot.error_code, "GOAL_REJECTED")


if __name__ == "__main__":
    unittest.main()
