"""Relay the scheduler's world to odometry/status; never invent independent motion."""

import json
import math
import time

from nav_msgs.msg import Odometry
from geometry_msgs.msg import TransformStamped
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import BatteryState, Image
from std_msgs.msg import String
from tf2_ros import StaticTransformBroadcaster

from robot_simulator.common import latched_qos, run_node


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
        # The simulation world uses identical map/odom origins. This explicit TF
        # is simulation infrastructure, never an assumption in the workstation.
        self.tf_broadcaster = StaticTransformBroadcaster(self)
        transform = TransformStamped()
        transform.header.stamp = self.get_clock().now().to_msg()
        transform.header.frame_id, transform.child_frame_id = "map", "odom"
        transform.transform.rotation.w = 1.0
        self.tf_broadcaster.sendTransform(transform)
        self.subscription = self.create_subscription(
            String, "/simulator/world_state", self.on_world, latched_qos()
        )
        self.camera_subscription = self.create_subscription(
            Image, "/camera/image_raw", self.on_camera, qos_profile_sensor_data
        )
        self.last_camera = None
        self.camera_stamp = 0
        self.world = None
        self.last_world = 0.0
        self.last_error_log = 0.0
        self.started = time.monotonic()
        self.timer = self.create_timer(0.1, self.publish)
        self.get_logger().info(
            "[SIM] 等待统一执行调度状态；里程计和物料状态不再独立巡航"
        )

    def on_camera(self, message):
        stamp = message.header.stamp.sec * 10**9 + message.header.stamp.nanosec
        if (
            stamp > self.camera_stamp
            and message.width > 0
            and message.height > 0
            and message.step > 0
            and len(message.data) >= message.step * message.height
        ):
            self.last_camera, self.camera_stamp = time.monotonic(), stamp

    def on_world(self, message):
        try:
            frame = json.loads(message.data)
            stamp = frame["stamp"]
            if (
                not isinstance(frame["boot_id"], str)
                or not frame["boot_id"]
                or not isinstance(frame["event_seq"], int)
                or frame["event_seq"] < 1
            ):
                raise ValueError("世界状态来源或序号无效")
            if (
                not isinstance(stamp["sec"], int)
                or stamp["sec"] < 0
                or not isinstance(stamp["nanosec"], int)
                or not 0 <= stamp["nanosec"] < 10**9
            ):
                raise ValueError("世界状态时间戳无效")
            if any(
                not isinstance(frame[key], (int, float))
                or not math.isfinite(frame[key])
                for key in ("x", "y", "yaw", "speed", "progress")
            ):
                raise ValueError("世界坐标、速度或进度无效")
            if frame["state"] not in (
                "IDLE",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "CANCELED",
            ):
                raise ValueError("世界状态无效")
            if not isinstance(frame["objects"], list) or not isinstance(
                frame["held_object_id"], str
            ):
                raise ValueError("物料状态无效")
            for item in frame["objects"]:
                if (
                    not isinstance(item["id"], str)
                    or not isinstance(item["station_id"], str)
                    or not isinstance(item["carried"], bool)
                    or any(
                        not isinstance(item[key], (int, float))
                        or not math.isfinite(item[key])
                        for key in ("x", "y")
                    )
                ):
                    raise ValueError("物料位置无效")
            carried = [item["id"] for item in frame["objects"] if item["carried"]]
            if carried != (
                [frame["held_object_id"]] if frame["held_object_id"] else []
            ):
                raise ValueError("持物标识与物料位置不一致")
            if (
                self.world
                and frame["boot_id"] == self.world["boot_id"]
                and frame["event_seq"] <= self.world["event_seq"]
            ):
                return
            self.world, self.last_world = frame, time.monotonic()
        except (ValueError, TypeError, KeyError) as exc:
            if time.monotonic() - self.last_error_log >= 2.0:
                self.get_logger().warning(f"[SIM] 忽略无效调度状态：{exc}")
                self.last_error_log = time.monotonic()

    def publish(self):
        elapsed = time.monotonic() - self.started
        stamp = self.get_clock().now().to_msg()
        fresh = self.world is not None and time.monotonic() - self.last_world < 2.0
        if self.world:
            frame = self.world
            odom = Odometry()
            odom.header.frame_id, odom.child_frame_id = "odom", "base_link"
            odom.header.stamp.sec = frame["stamp"]["sec"]
            odom.header.stamp.nanosec = frame["stamp"]["nanosec"]
            odom.pose.pose.position.x, odom.pose.pose.position.y = float(
                frame["x"]
            ), float(frame["y"])
            odom.pose.pose.orientation.z, odom.pose.pose.orientation.w = math.sin(
                frame["yaw"] / 2
            ), math.cos(frame["yaw"] / 2)
            odom.twist.twist.linear.x = float(frame["speed"]) if fresh else 0.0
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
        status = dict(self.world or {})
        if self.world:
            status["world_stamp"] = self.world["stamp"]
        state_name = {
            "IDLE": "待命",
            "RUNNING": "执行中",
            "SUCCEEDED": "已完成",
            "FAILED": "失败",
            "CANCELED": "已取消",
        }.get(status.get("state"), "待命")
        status.update(
            simulator=True,
            stamp={"sec": stamp.sec, "nanosec": stamp.nanosec},
            speed=float(self.world["speed"]) if fresh else 0.0,
            world_fresh=fresh,
            chassis_ready=fresh and status.get("state") != "FAILED",
            camera_ready=(
                self.last_camera is not None
                and time.monotonic() - self.last_camera < 2.0
            ),
            mode=(
                ("统一模拟执行 · " + state_name)
                if fresh
                else "调度状态未就绪／已失联，位置保留，速度归零"
            ),
        )
        self.status_pub.publish(String(data=json.dumps(status, ensure_ascii=False)))


def main(args=None):
    run_node(RobotStateSimulator, args)
