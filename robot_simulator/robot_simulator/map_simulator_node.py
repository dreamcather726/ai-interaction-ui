"""Transient-local occupancy grid and metadata; no SLAM hardware."""

from nav_msgs.msg import MapMetaData, OccupancyGrid
from rclpy.node import Node

from robot_simulator.common import latched_qos, run_node
from robot_simulator.scene import occupancy_grid


class MapSimulator(Node):
    def __init__(self):
        super().__init__("map")
        self.declare_parameter("catalog_path", "")
        show_labels = not bool(self.get_parameter("catalog_path").value)
        self.map_pub = self.create_publisher(OccupancyGrid, "/map", latched_qos())
        self.metadata_pub = self.create_publisher(
            MapMetaData, "/map_metadata", latched_qos()
        )
        message = OccupancyGrid()
        message.header.frame_id = "map"
        message.header.stamp = self.get_clock().now().to_msg()
        message.info.map_load_time = message.header.stamp
        message.info.resolution = 0.05
        message.info.width = message.info.height = 200
        message.info.origin.position.x = message.info.origin.position.y = -5.0
        message.info.origin.orientation.w = 1.0
        message.data = occupancy_grid(show_labels=show_labels)
        self.map_pub.publish(message)
        self.metadata_pub.publish(message.info)
        self.get_logger().info(
            "[SIM] 10m × 10m 地图已发布；工位标记由执行场景提供，支持迟到订阅者"
        )


def main(args=None):
    run_node(MapSimulator, args)
