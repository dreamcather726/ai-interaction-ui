"""Frame/encoding/unknown-state contracts. Uses no running ROS nodes."""

import math
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from robot_visualization.geometry import grid_point, quaternion, rotate
from robot_visualization.connection import RobotConnection

try:
    from geometry_msgs.msg import TransformStamped
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import BatteryState, Image
    from robot_visualization.ros.telemetry import RobotTelemetry

    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False


class GeometryContractTests(unittest.TestCase):
    def test_rotated_map_origin_is_inverted_before_scaling(self):
        x, y = grid_point(1.0, 3.0, 1.0, 1.0, math.pi / 2, 0.5)
        self.assertAlmostEqual(x, 4.0)
        self.assertAlmostEqual(y, 0.0)

    def test_quaternion_rotation_does_not_assume_aligned_frames(self):
        q = SimpleNamespace(
            x=0.0, y=0.0, z=math.sin(math.pi / 4), w=math.cos(math.pi / 4)
        )
        x, y, z = rotate((1.0, 0.0, 0.0), q)
        self.assertAlmostEqual(x, 0.0)
        self.assertAlmostEqual(y, 1.0)
        self.assertAlmostEqual(z, 0.0)

    def test_missing_orientation_is_rejected(self):
        with self.assertRaises(ValueError):
            quaternion(SimpleNamespace(x=0.0, y=0.0, z=0.0, w=0.0))


@unittest.skipUnless(ROS_AVAILABLE, "需要已构建的ROS接口；不启动节点")
class TelemetryContractTests(unittest.TestCase):
    def setUp(self):
        self.client = RobotTelemetry.__new__(RobotTelemetry)
        self.client.config = RobotConnection()
        self.client.events, self.client.buffer = Mock(), Mock()
        self.client.map_frame = "map"
        self.client.last_odom = self.client.odom_time = None
        self.client.last_position_poll = 0.0
        self.client.errors = {}

    def odom(self):
        message = Odometry()
        message.header.frame_id = "odom"
        message.header.stamp.sec = 100
        message.pose.pose.position.x = 1.0
        message.pose.pose.orientation.w = 1.0
        return message

    def test_timestamped_tf_is_used_for_map_overlay(self):
        tf = TransformStamped()
        tf.transform.rotation.z = math.sin(math.pi / 4)
        tf.transform.rotation.w = math.cos(math.pi / 4)
        tf.transform.translation.x = 5.0
        self.client.buffer.lookup_transform.return_value = tf
        self.client.on_odom(self.odom())
        value = self.client.events.record.emit.call_args.args[1]
        self.assertTrue(value["map_valid"])
        self.assertAlmostEqual(value["map_x"], 5.0)
        self.assertAlmostEqual(value["map_y"], 1.0)
        args = self.client.buffer.lookup_transform.call_args.args
        self.assertEqual(args[:2], ("map", "odom"))
        self.assertEqual(args[2].nanoseconds, 100 * 10**9)

    def test_missing_tf_keeps_odom_but_does_not_invent_map_position(self):
        self.client.buffer.lookup_transform.side_effect = ValueError("missing TF")
        self.client.on_odom(self.odom())
        value = self.client.events.record.emit.call_args.args[1]
        self.assertFalse(value["map_valid"])
        self.assertNotIn("map_x", value)
        self.assertEqual(value["x"], 1.0)

    def test_bgr_image_keeps_encoding_and_row_padding(self):
        image = Image()
        image.width, image.height, image.step, image.encoding = 2, 2, 8, "bgr8"
        image.data = bytes(range(16))
        self.client.on_camera(image)
        kind, value = self.client.events.record.emit.call_args.args
        self.assertEqual(kind, "robot_camera")
        self.assertEqual((value["encoding"], value["step"]), ("bgr8", 8))

    def test_unsupported_image_is_reported_instead_of_misrendered(self):
        image = Image()
        image.encoding = "16UC1"
        self.client.on_camera(image)
        self.assertEqual(
            self.client.events.record.emit.call_args.args[0], "robot_camera_error"
        )

    def test_unknown_percentage_does_not_hide_known_voltage_or_turn_into_zero(self):
        battery = BatteryState()
        battery.present = True
        battery.percentage, battery.voltage = float("nan"), 24.0
        self.client.on_battery(battery)
        value = self.client.events.record.emit.call_args.args[1]
        self.assertIsNone(value["percentage"])
        self.assertEqual(value["voltage"], 24.0)
