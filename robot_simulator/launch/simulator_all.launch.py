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
        DeclareLaunchArgument("linear_speed", default_value="0.8"),
        DeclareLaunchArgument("step_timeout", default_value="60.0"),
        DeclareLaunchArgument("fail_step", default_value="0"),
        DeclareLaunchArgument("catalog_path", default_value=""),
        DeclareLaunchArgument("feedback_period", default_value="1.0"),
        DeclareLaunchArgument(
            "stations_json",
            default_value='{"station_a":[-2.0,0.0],"station_b":[2.0,0.0]}',
        ),
        DeclareLaunchArgument("objects_json", default_value='{"box_1":"station_a"}'),
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
        (
            "map_simulator_node",
            "map",
            {
                "catalog_path": ParameterValue(
                    LaunchConfiguration("catalog_path"), value_type=str
                )
            },
        ),
        ("camera_simulator_node", "camera_raw", camera),
        ("robot_state_simulator_node", "robot_state_node", {}),
        (
            "execute_task_action_server",
            "execute_task_action_server",
            {
                "catalog_path": ParameterValue(
                    LaunchConfiguration("catalog_path"), value_type=str
                ),
                "feedback_period": ParameterValue(
                    LaunchConfiguration("feedback_period"), value_type=float
                ),
                "step_seconds": ParameterValue(
                    LaunchConfiguration("step_seconds"), value_type=float
                ),
                "linear_speed": ParameterValue(
                    LaunchConfiguration("linear_speed"), value_type=float
                ),
                "step_timeout": ParameterValue(
                    LaunchConfiguration("step_timeout"), value_type=float
                ),
                "fail_step": ParameterValue(
                    LaunchConfiguration("fail_step"), value_type=int
                ),
                "stations_json": ParameterValue(
                    LaunchConfiguration("stations_json"), value_type=str
                ),
                "objects_json": ParameterValue(
                    LaunchConfiguration("objects_json"), value_type=str
                ),
            },
        ),
        ("play_audio_action_server", "play_audio_action_server", {}),
    ):
        nodes.append(
            Node(
                package="robot_simulator",
                executable=executable,
                name=node_name,
                namespace="/camera" if executable == "camera_simulator_node" else "/",
                output="screen",
                parameters=[parameters],
            )
        )
    return LaunchDescription(arguments + nodes)
