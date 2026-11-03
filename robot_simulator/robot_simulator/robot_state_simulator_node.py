"""Patrol odometry, slowly discharging battery and explicitly simulated status."""

import json
import math
import time

from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import BatteryState
from std_msgs.msg import String

from robot_simulator.common import run_node
from robot_simulator.scene import STATIONS, patrol


class RobotStateSimulator(Node):
    def __init__(self):
        super().__init__("robot_state_node")
        self.declare_parameter("battery_drain_seconds", 3600.0)
        self.drain_seconds = self.get_parameter("battery_drain_seconds").value
        if self.drain_seconds <= 0:
            raise ValueError("battery_drain_seconds 必须大于 0")
        self.odom_pub = self.create_publisher(
            Odometry, "/odom", qos_profile_sensor_data
        )
        self.battery_pub = self.create_publisher(
            BatteryState, "/battery", qos_profile_sensor_data
        )
        self.battery_alias = self.create_publisher(
            BatteryState, "/battery_state", qos_profile_sensor_data
        )
        self.status_pub = self.create_publisher(String, "/robot_status", 10)
        self.started = time.monotonic()
        self.timer = self.create_timer(0.1, self.publish)
        self.get_logger().info("[SIM] 底盘巡航、电池和网络状态已启动；没有真实运动输出")

    def publish(self):
        elapsed = time.monotonic() - self.started
        stamp = self.get_clock().now().to_msg()
        x, y, yaw, speed = patrol(elapsed)
        odom = Odometry()
        odom.header.stamp, odom.header.frame_id = stamp, "odom"
        odom.child_frame_id = "base_link"
        odom.pose.pose.position.x, odom.pose.pose.position.y = x, y
        odom.pose.pose.orientation.z, odom.pose.pose.orientation.w = math.sin(
            yaw / 2
        ), math.cos(yaw / 2)
        odom.twist.twist.linear.x = speed
        odom.twist.twist.linear.y = 0.02 * math.cos(elapsed) * math.cos(yaw)
        for index in (0, 7, 35):
            odom.pose.covariance[index] = 0.002
            odom.twist.covariance[index] = 0.001
        self.odom_pub.publish(odom)
        battery = BatteryState()
        battery.header.stamp = stamp
        battery.header.frame_id = "base_link"
        battery.percentage = max(0.2, 1.0 - 0.8 * elapsed / self.drain_seconds)
        battery.voltage = 22.0 + 3.2 * battery.percentage + 0.03 * math.sin(elapsed)
        battery.current = -1.8 - 0.15 * math.sin(elapsed * 0.4)
        battery.temperature = 29.0 + 0.8 * math.sin(elapsed * 0.02)
        battery.capacity = battery.design_capacity = 10.0
        battery.charge = battery.capacity * battery.percentage
        battery.present = True
        battery.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_DISCHARGING
        battery.power_supply_health = BatteryState.POWER_SUPPLY_HEALTH_GOOD
        battery.power_supply_technology = BatteryState.POWER_SUPPLY_TECHNOLOGY_LION
        self.battery_pub.publish(battery)
        self.battery_alias.publish(battery)
        self.status_pub.publish(
            String(
                data=json.dumps(
                    {
                        "simulator": True,
                        "stamp": {"sec": stamp.sec, "nanosec": stamp.nanosec},
                        "chassis_ready": True,
                        "camera_ready": True,
                        "network_connected": True,
                        "network_latency_ms": round(
                            8.0 + 2 * math.sin(elapsed * 0.8), 1
                        ),
                        "ip": "127.0.0.1",
                        "mode": "模拟巡航（独立于任务演示）",
                        "stations": STATIONS,
                    },
                    ensure_ascii=False,
                )
            )
        )


def main(args=None):
    run_node(RobotStateSimulator, args)
