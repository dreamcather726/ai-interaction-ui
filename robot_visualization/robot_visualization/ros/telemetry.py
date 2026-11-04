"""Typed robot telemetry and timestamped TF, identical for every deployment."""

import math
import time

from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.duration import Duration
from rclpy.qos import (
    DurabilityPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from rclpy.time import Time
from robot_interfaces.msg import MaterialState, NodeHeartbeat, RobotStatus, TaskSnapshot
from sensor_msgs.msg import BatteryState, Image
from tf2_ros import Buffer, TransformException, TransformListener

from robot_visualization.geometry import quaternion, rotate, yaw


def sample_time(stamp):
    return dict(
        received_at=time.monotonic(), stamp_ns=stamp.sec * 10**9 + stamp.nanosec
    )


class RobotTelemetry:
    def __init__(
        self,
        node,
        events,
        config,
        status_callback,
        heartbeat_callback,
        snapshot_callback,
    ):
        self.node, self.events, self.config = node, events, config
        self.map_frame = ""
        self.last_odom = None
        self.odom_time = None
        self.last_position_poll = 0.0
        self.errors = {}
        self.buffer = Buffer()
        self.listener = TransformListener(self.buffer, node, spin_thread=False)
        latched = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.subscriptions = [
            node.create_subscription(
                OccupancyGrid, config.map_topic, self.on_map, latched
            ),
            node.create_subscription(
                Image, config.camera_topic, self.on_camera, qos_profile_sensor_data
            ),
            node.create_subscription(
                Odometry, config.odom_topic, self.on_odom, qos_profile_sensor_data
            ),
            node.create_subscription(
                BatteryState,
                config.battery_topic,
                self.on_battery,
                qos_profile_sensor_data,
            ),
            node.create_subscription(
                RobotStatus, config.status_topic, status_callback, 10
            ),
            node.create_subscription(
                NodeHeartbeat, config.heartbeat_topic, heartbeat_callback, 10
            ),
            node.create_subscription(
                TaskSnapshot, config.snapshot_topic, snapshot_callback, latched
            ),
            node.create_subscription(
                MaterialState, config.material_topic, self.on_material, 10
            ),
        ]

    def error(self, kind, message):
        if self.errors.get(kind) != message:
            self.errors[kind] = message
            self.events.report_log("[W] " + message)
        self.events.record.emit("robot_" + kind + "_error", {"message": message})

    def on_map(self, message):
        info = message.info
        try:
            q = quaternion(info.origin.orientation)
            if abs(q[0]) > 1e-4 or abs(q[1]) > 1e-4:
                raise ValueError("仅支持平面栅格原点姿态")
            if (
                not message.header.frame_id
                or info.width < 1
                or info.height < 1
                or info.width * info.height != len(message.data)
                or not math.isfinite(info.resolution)
                or info.resolution <= 0
                or not all(
                    math.isfinite(v)
                    for v in (info.origin.position.x, info.origin.position.y)
                )
            ):
                raise ValueError("栅格尺寸、原点、frame 或分辨率无效")
            self.map_frame = message.header.frame_id
            self.events.record.emit(
                "robot_map",
                dict(
                    frame_id=self.map_frame,
                    width=info.width,
                    height=info.height,
                    resolution=info.resolution,
                    origin_x=info.origin.position.x,
                    origin_y=info.origin.position.y,
                    origin_yaw=yaw(info.origin.orientation),
                    data=list(message.data),
                    **sample_time(message.header.stamp),
                ),
            )
            self.poll_position(force=True)
        except ValueError as exc:
            self.map_frame = ""
            self.error("map", f"地图不可用：{exc}")

    def on_camera(self, message):
        channels = {"rgb8": 3, "bgr8": 3, "mono8": 1, "rgba8": 4, "bgra8": 4}
        count = channels.get(message.encoding)
        if (
            not count
            or message.width < 1
            or message.height < 1
            or message.step < message.width * count
            or len(message.data) < message.step * message.height
        ):
            self.error("camera", "相机编码或帧数据不支持：" + message.encoding)
            return
        self.errors.pop("camera", None)
        self.events.record.emit(
            "robot_camera",
            dict(
                width=message.width,
                height=message.height,
                step=message.step,
                encoding=message.encoding,
                data=bytes(message.data),
                **sample_time(message.header.stamp),
            ),
        )

    def on_odom(self, message):
        values = (
            message.pose.pose.position.x,
            message.pose.pose.position.y,
            message.pose.pose.position.z,
            message.twist.twist.linear.x,
            message.twist.twist.angular.z,
        )
        try:
            if not message.header.frame_id or not all(math.isfinite(v) for v in values):
                raise ValueError("里程计坐标、速度或 frame 无效")
            quaternion(message.pose.pose.orientation)
        except ValueError as exc:
            self.error("odom", str(exc))
            return
        self.last_odom, self.odom_time = message, sample_time(message.header.stamp)
        self.poll_position(force=True)

    def poll_position(self, *, force=False):
        now = time.monotonic()
        if (
            not self.last_odom
            or not self.odom_time
            or now - self.odom_time["received_at"] >= self.config.telemetry_timeout
        ):
            return
        if not force and now - self.last_position_poll < 0.2:
            return
        self.last_position_poll = now
        message = self.last_odom
        p = message.pose.pose.position
        value = dict(
            x=p.x,
            y=p.y,
            yaw=yaw(message.pose.pose.orientation),
            frame_id=message.header.frame_id,
            map_frame=self.map_frame,
            map_valid=False,
            speed=message.twist.twist.linear.x,
            angular=message.twist.twist.angular.z,
            **self.odom_time,
        )
        try:
            if not self.map_frame:
                raise ValueError("等待有效地图坐标系")
            if message.header.frame_id == self.map_frame:
                value.update(map_x=p.x, map_y=p.y, map_yaw=value["yaw"], map_valid=True)
            else:
                if value["stamp_ns"] <= 0:
                    raise ValueError("里程计缺少有效采样时间，无法对齐 TF")
                tf = self.buffer.lookup_transform(
                    self.map_frame,
                    message.header.frame_id,
                    Time.from_msg(message.header.stamp),
                    timeout=Duration(seconds=0),
                )
                transform = tf.transform
                rotation = quaternion(transform.rotation)
                if abs(rotation[0]) > 1e-4 or abs(rotation[1]) > 1e-4:
                    raise ValueError("二维地图暂不支持倾斜的定位坐标系")
                x, y, _ = rotate((p.x, p.y, p.z), transform.rotation)
                value.update(
                    map_x=x + transform.translation.x,
                    map_y=y + transform.translation.y,
                    map_yaw=value["yaw"] + yaw(transform.rotation),
                    map_valid=True,
                )
            if not all(
                math.isfinite(value[key]) for key in ("map_x", "map_y", "map_yaw")
            ):
                raise ValueError("变换后的地图位置无效")
            value["position_message"] = ""
        except (TransformException, ValueError) as exc:
            value["map_valid"] = False
            for key in ("map_x", "map_y", "map_yaw"):
                value.pop(key, None)
            value["position_message"] = "地图定位未确认：" + str(exc)
        self.events.record.emit("robot_odom", value)

    def on_battery(self, message):
        self.events.record.emit(
            "robot_battery",
            dict(
                percentage=(
                    message.percentage
                    if message.present
                    and math.isfinite(message.percentage)
                    and 0 <= message.percentage <= 1
                    else None
                ),
                voltage=(
                    message.voltage
                    if message.present
                    and math.isfinite(message.voltage)
                    and message.voltage > 0
                    else None
                ),
                **sample_time(message.header.stamp),
            ),
        )

    def on_material(self, message):
        if message.source_node != self.config.executor_node:
            return
        identities = [item.object_id for item in message.objects]
        carried = [item.object_id for item in message.objects if item.carried]
        if (
            not message.boot_id
            or message.event_seq < 1
            or any(not identity for identity in identities)
            or len(set(identities)) != len(identities)
            or (
                message.known
                and carried
                != ([message.held_object_id] if message.held_object_id else [])
            )
        ):
            self.error("material", "物料状态的来源、序号、ID或持物清单不一致")
            return
        objects = []
        for item in message.objects:
            known = item.position_known and all(
                math.isfinite(v)
                for v in (item.position.x, item.position.y, item.position.z)
            )
            objects.append(
                dict(
                    id=item.object_id,
                    station_id=item.station_id,
                    carried=item.carried,
                    position_known=known,
                    x=item.position.x,
                    y=item.position.y,
                )
            )
        self.events.record.emit(
            "robot_material",
            dict(
                known=message.known,
                boot_id=message.boot_id,
                event_seq=message.event_seq,
                frame_id=message.frame_id,
                held_object_id=message.held_object_id,
                objects=objects,
                **sample_time(message.stamp),
            ),
        )

    def close(self):
        self.listener.unregister()
