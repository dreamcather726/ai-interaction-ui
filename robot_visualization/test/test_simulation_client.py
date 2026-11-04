"""Client protocol regressions using controlled futures/time; no nodes or hardware."""

from concurrent.futures import Future
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from builtin_interfaces.msg import Time
    from robot_interfaces.action import ExecuteTask
    from robot_interfaces.srv import PrepareTask
    from robot_visualization.ros.simulation import SimulationClient

    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False


@unittest.skipUnless(ROS_AVAILABLE, "需要构建后的 ROS 2 接口，不启动节点")
class SimulationClientTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        clock = patch(
            "robot_visualization.ros.simulation.time.monotonic",
            side_effect=lambda: self.now,
        )
        clock.start()
        self.addCleanup(clock.stop)
        self.action = Mock()
        action_type = patch(
            "robot_visualization.ros.simulation.ActionClient", return_value=self.action
        )
        action_type.start()
        self.addCleanup(action_type.stop)
        self.node, self.events = Mock(), Mock()
        self.node.get_clock.return_value.now.return_value.to_msg.return_value = Time(
            sec=100
        )
        self.prepare = self.node.create_client.return_value
        self.prepare.service_is_ready.return_value = True
        self.action.server_is_ready.return_value = True
        self.client = SimulationClient(self.node, self.events)
        self.draft = dict(
            draft_id="draft-one",
            revision=1,
            state=1,
            session_id="session-one",
            input_context_id="context-one",
            candidate=dict(task_type="NAVIGATE", destination_station_id="station_a"),
        )

    def last_task(self):
        return self.events.record.emit.call_args.args[1]

    def start_prepare(self):
        self.prepare_future = Future()
        self.prepare.call_async.return_value = self.prepare_future
        self.client.execute(self.draft)
        return self.prepare.call_async.call_args.args[0]

    def accept_prepare(self):
        request = self.start_prepare()
        response = PrepareTask.Response()
        response.success = True
        response.request_id = request.request_id
        response.task = request.task
        response.revision = 1
        response.confirmation_context_id = request.confirmation_context_id
        self.goal_future = Future()
        self.action.send_goal_async.return_value = self.goal_future
        self.prepare_future.set_result(response)
        self.client.poll()

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
        feedback = ExecuteTask.Feedback()
        feedback.request_id = self.client.goal.request_id
        feedback.task_id = task_id or self.client.goal.task_id
        feedback.dispatch_id = self.client.goal.dispatch_id
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
        result.success = not canceled
        result.task.state = result.task.CANCELED if canceled else result.task.SUCCEEDED
        result.task.progress = 0.25 if canceled else 1.0
        result.error_code = "CANCELED" if canceled else ""
        result.message = "模拟执行已取消" if canceled else "模拟执行完成"
        self.result_future.set_result(SimpleNamespace(result=result))
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
        self.action.send_goal_async.side_effect = RuntimeError("transport unavailable")
        self.prepare_future.set_result(response)
        self.client.poll()
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


if __name__ == "__main__":
    unittest.main()
