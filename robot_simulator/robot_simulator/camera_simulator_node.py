"""RGB test frames generated in memory, with no camera/device access."""

import time
from datetime import datetime

from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from robot_simulator.common import run_node
from robot_simulator.scene import rgb_frame


class CameraSimulator(Node):
    def __init__(self):
        super().__init__("camera_raw", namespace="/camera")
        for name, value in (("width", 640), ("height", 480), ("fps", 10.0)):
            self.declare_parameter(name, value)
        self.width = self.get_parameter("width").value
        self.height = self.get_parameter("height").value
        fps = self.get_parameter("fps").value
        if not (
            32 <= self.width <= 1920 and 32 <= self.height <= 1080 and 0 < fps <= 30
        ):
            raise ValueError("width/height 范围 32～1920/1080；fps 必须在 (0,30] 内")
        self.publisher = self.create_publisher(
            Image, "/camera/image_raw", qos_profile_sensor_data
        )
        self.started = time.monotonic()
        self.timer = self.create_timer(1.0 / fps, self.publish)
        self.get_logger().info(
            f"[SIM] 相机 {self.width}×{self.height} rgb8，{fps:g} fps"
        )

    def publish(self):
        message = Image()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = "camera_optical_frame"
        message.width, message.height = self.width, self.height
        message.encoding = "rgb8"
        message.is_bigendian = 0
        message.step = self.width * 3
        message.data = rgb_frame(
            self.width,
            self.height,
            time.monotonic() - self.started,
            datetime.now().strftime("%H:%M:%S"),
        )
        self.publisher.publish(message)


def main(args=None):
    run_node(CameraSimulator, args)
