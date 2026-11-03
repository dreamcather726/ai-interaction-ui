"""Start the five hardware-free nodes, without audio capture or playback devices."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    arguments = [
        DeclareLaunchArgument("camera_width", default_value="640"),
        DeclareLaunchArgument("camera_height", default_value="480"),
        DeclareLaunchArgument("camera_fps", default_value="10.0"),
        DeclareLaunchArgument("step_seconds", default_value="2.0"),
    ]
    camera = {
        name: ParameterValue(LaunchConfiguration(argument), value_type=kind)
        for name, argument, kind in (
            ("width", "camera_width", int),
            ("height", "camera_height", int),
            ("fps", "camera_fps", float),
        )
    }
    nodes = []
    for executable, node_name, parameters in (
        ("map_simulator_node", "map_node", {}),
        ("camera_simulator_node", "camera_node", camera),
        ("robot_state_simulator_node", "robot_state_node", {}),
        (
            "execute_task_action_server",
            "execute_task_action_server",
            {
                "step_seconds": ParameterValue(
                    LaunchConfiguration("step_seconds"), value_type=float
                )
            },
        ),
        ("play_audio_action_server", "play_audio_action_server", {}),
    ):
        nodes.append(
            Node(
                package="robot_simulator",
                executable=executable,
                name=node_name,
                output="screen",
                parameters=[parameters],
            )
        )
    return LaunchDescription(arguments + nodes)
