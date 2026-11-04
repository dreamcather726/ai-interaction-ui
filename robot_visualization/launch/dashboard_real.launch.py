"""Real-device deployment; starts AI/audio helpers, never the simulator."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument("domain_id", default_value="72"),
            DeclareLaunchArgument(
                "params_file",
                default_value=PathJoinSubstitution(
                    [FindPackageShare("robot_visualization"), "config", "real.yaml"]
                ),
            ),
            Node(
                package="robot_visualization",
                executable="dashboard",
                output="screen",
                prefix="/usr/bin/python3",
                parameters=[LaunchConfiguration("params_file")],
                additional_env={"ROS_DOMAIN_ID": LaunchConfiguration("domain_id")},
            ),
        ]
    )
