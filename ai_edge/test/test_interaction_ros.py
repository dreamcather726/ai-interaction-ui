"""真实ROS Action/Service往返，使用受控模型输出和遥测夹具，不驱动机器人。"""

import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

try:
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.action import ActionClient
    from rclpy.qos import qos_profile_sensor_data
    from nav_msgs.msg import Odometry
    from robot_interfaces.action import ParseTask
    from robot_interfaces.msg import DialogueTurn
    from ai_edge.semantic.parse_task_node import ParseTaskNode
    from ai_edge.semantic.robot_state_query_node import RobotStateQueryNode
    from ai_edge.semantic.interaction_contract import InteractionAnswer
    from ai_edge.semantic.semantic_contract import empty_candidate, validate_candidate
    from ai_edge.semantic.ros_candidates import assign_candidate
    from ai_edge.audio.audio_result_validation import validate_parse_result
    from ai_edge.audio.audio_pipeline import AudioPipeline
    from ai_edge.semantic.dialogue import DialogueStore

    ROS_AVAILABLE = hasattr(ParseTask.Result, "RESPONDED")
except ImportError:
    ROS_AVAILABLE = False


@unittest.skipUnless(ROS_AVAILABLE, "需要构建0.4.0 ROS接口")
class InteractionROSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.parser = ParseTaskNode()
        self.query = RobotStateQueryNode()
        self.driver = Node("interaction_test_driver")
        self.executor = SingleThreadedExecutor()
        for node in (self.parser, self.query, self.driver):
            self.executor.add_node(node)
        self.client = ActionClient(self.driver, ParseTask, "/ai/parse_task")
        self.odom = self.driver.create_publisher(
            Odometry, "/odom", qos_profile_sensor_data
        )
        self.pump(0.6)
        self.assertTrue(self.client.server_is_ready())

    def tearDown(self):
        self.client.destroy()
        self.parser.close()
        for node in (self.driver, self.query, self.parser):
            self.executor.remove_node(node)
            node.destroy_node()
        self.executor.shutdown()

    def pump(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.executor.spin_once(timeout_sec=0.01)

    def await_future(self, future):
        deadline = time.monotonic() + 8
        while not future.done() and time.monotonic() < deadline:
            self.executor.spin_once(timeout_sec=0.02)
        self.assertTrue(future.done(), "ROS请求未在8秒内完成")
        return future.result()

    def goal(self, text):
        goal = ParseTask.Goal()
        goal.stamp = self.driver.get_clock().now().to_msg()
        goal.request_id = "test-" + uuid4().hex
        goal.session_id, goal.input_context_id = "test-session", "test-context"
        goal.input_id, goal.text = "audio:test-segment", text
        goal.allowed_station_ids = ["warehouse_entry", "packing_room"]
        return goal

    def call(self, goal):
        handle = self.await_future(self.client.send_goal_async(goal))
        self.assertTrue(handle.accepted)
        wrapped = self.await_future(handle.get_result_async())
        source = SimpleNamespace(
            request_id=goal.request_id,
            session_id=goal.session_id,
            input_context_id=goal.input_context_id,
            segment_id="test-segment",
        )
        validate_parse_result(source, wrapped, goal.allowed_station_ids, [])
        return wrapped.result

    def test_chat_with_pending_candidate_returns_reply_without_executable_task(self):
        partial = empty_candidate("TRANSPORT")
        partial.update(
            pickup_station_id="warehouse_entry", dropoff_station_id="packing_room"
        )
        goal = self.goal("你好")
        assign_candidate(goal.partial_candidate, partial, goal.stamp)
        turn = DialogueTurn()
        turn.role, turn.text = turn.ASSISTANT, "要搬运哪个物料？"
        goal.context = [turn]
        self.parser.backend.interact = lambda *args: InteractionAnswer("CHAT", "你好！")
        result = self.call(goal)
        self.assertEqual(result.outcome, result.RESPONDED)
        self.assertFalse(result.success)
        self.assertEqual(result.reply, "你好！")
        self.assertEqual(result.candidate.task_type, result.candidate.UNKNOWN)
        store = DialogueStore()
        key = (goal.session_id, goal.input_context_id)
        store.remember(
            key,
            "去仓库拿东西到包装间放下",
            validate_candidate(partial, goal.allowed_station_ids, []),
            1,
        )
        previous = store.pending[key]
        pipeline = SimpleNamespace(
            dialogues=store, dialogue_key=AudioPipeline.dialogue_key
        )
        job = dict(msg=goal, dialogue=previous)
        AudioPipeline.update_dialogue(
            pipeline, job, result, goal.allowed_station_ids, []
        )
        self.assertIs(store.pending[key], previous)
        self.assertIn("仍保留", result.message)

    def test_location_query_uses_actual_service_measurement(self):
        for _ in range(3):
            odom = Odometry()
            odom.header.stamp = self.driver.get_clock().now().to_msg()
            odom.header.frame_id = "test_odom"
            odom.pose.pose.position.x, odom.pose.pose.position.y = 1.25, -2.5
            odom.pose.pose.orientation.w = 1.0
            self.odom.publish(odom)
            self.pump(0.05)
        self.parser.backend.interact = lambda *args: InteractionAnswer(
            "QUERY", "模型编造的位置", ("ROBOT_LOCATION",)
        )
        result = self.call(self.goal("你在哪里？"))
        self.assertEqual(result.outcome, result.RESPONDED)
        self.assertEqual(result.error_code, "")
        self.assertIn("1.25", result.reply)
        self.assertIn("test_odom", result.reply)
        self.assertNotIn("模型编造", result.reply)
        self.assertIn('"success": true', result.query_result_json)

    def test_absent_measurement_is_an_explicit_reply(self):
        self.parser.backend.interact = lambda *args: InteractionAnswer(
            "QUERY", "", ("ROBOT_BATTERY",)
        )
        result = self.call(self.goal("剩多少电？"))
        self.assertEqual(result.outcome, result.RESPONDED)
        self.assertEqual(result.error_code, "ROBOT_DATA_UNAVAILABLE")
        self.assertIn("暂时无法查询", result.reply)

    def test_normal_navigation_still_produces_candidate(self):
        candidate = empty_candidate("NAVIGATE")
        candidate["destination_station_id"] = "packing_room"
        answer = validate_candidate(candidate, ["warehouse_entry", "packing_room"], [])
        self.parser.backend.interact = lambda *args: InteractionAnswer(
            "TASK", "", (), answer
        )
        result = self.call(self.goal("去包装间"))
        self.assertEqual(result.outcome, result.READY)
        self.assertTrue(result.success)
        self.assertEqual(result.candidate.destination_station_id, "packing_room")


if __name__ == "__main__":
    unittest.main()
