"""为对话提供真实的只读请求接口，遥测缺失、过期时明确失败。"""

import json
import math

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from nav_msgs.msg import Odometry
from sensor_msgs.msg import BatteryState
from robot_interfaces.msg import RobotStatus
from robot_interfaces.srv import QueryRobotState

from ai_edge.semantic.robot_state_query import RobotStateCache


def stamp_ns(stamp):
    return stamp.sec * 10**9 + stamp.nanosec


class RobotStateQueryNode(Node):
    def __init__(self):
        super().__init__("robot_state_query_node")
        for name, default in (
            ("odom_topic", "/odom"),
            ("status_topic", "/robot/status"),
            ("battery_topic", "/battery_state"),
            ("max_data_age", 2.0),
        ):
            self.declare_parameter(name, default)
        age = self.get_parameter("max_data_age").value
        if not 0.1 <= age <= 300:
            raise ValueError("max_data_age 必须在[0.1,300]秒内")
        self.cache = RobotStateCache(age)
        self.topics = {
            key: self.get_parameter(key + "_topic").value
            for key in ("odom", "status", "battery")
        }
        self.subscriptions_list = [
            self.create_subscription(
                Odometry, self.topics["odom"], self.on_odom, qos_profile_sensor_data
            ),
            self.create_subscription(
                BatteryState,
                self.topics["battery"],
                self.on_battery,
                qos_profile_sensor_data,
            ),
            self.create_subscription(
                RobotStatus, self.topics["status"], self.on_status, 10
            ),
        ]
        self.service = self.create_service(
            QueryRobotState, "/ai/query_robot_state", self.query
        )
        self.get_logger().info("机器人只读查询服务已启动；等待真实定位、状态和电池数据")

    def on_odom(self, message):
        position, q = message.pose.pose.position, message.pose.pose.orientation
        norm = math.sqrt(q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w)
        yaw = float("nan")
        if math.isfinite(norm) and norm > 1e-6:
            x, y, z, w = (q.x / norm, q.y / norm, q.z / norm, q.w / norm)
            yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
        self.cache.update(
            "ROBOT_LOCATION",
            dict(
                x=position.x,
                y=position.y,
                z=position.z,
                yaw=yaw,
                frame_id=message.header.frame_id,
            ),
            stamp_ns(message.header.stamp),
            self.topics["odom"],
        )

    def on_battery(self, message):
        self.cache.update(
            "ROBOT_BATTERY",
            dict(percentage=message.percentage),
            stamp_ns(message.header.stamp),
            self.topics["battery"],
        )

    def on_status(self, message):
        self.cache.update(
            "ROBOT_STATUS",
            dict(
                state=message.state,
                active_task_id=message.active_task_id,
                error_code=message.error_code,
            ),
            stamp_ns(message.stamp),
            self.topics["status"],
        )

    def query(self, request, response):
        response.stamp = self.get_clock().now().to_msg()
        response.request_id = request.request_id
        if not request.request_id.strip():
            response.error_code, response.reply = (
                "INVALID_REQUEST",
                "查询请求ID不能为空。",
            )
            return response
        success, code, reply, data = self.cache.query(
            request.query_type, stamp_ns(response.stamp)
        )
        response.success, response.error_code = success, code
        response.message = response.reply = reply
        response.data_json = json.dumps(data, ensure_ascii=False, allow_nan=False)
        return response


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = RobotStateQueryNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
