"""Add a file-backed microphone; wav_file is required, never invented speech."""

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "wav_file", description="Absolute path to 16kHz mono PCM s16le WAV"
            ),
            DeclareLaunchArgument("auto_play", default_value="true"),
            DeclareLaunchArgument("loop", default_value="true"),
            DeclareLaunchArgument("catalog_path", default_value=""),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    get_package_share_directory("robot_simulator")
                    + "/launch/simulator_all.launch.py"
                ),
                launch_arguments={
                    "catalog_path": LaunchConfiguration("catalog_path")
                }.items(),
            ),
            Node(
                package="robot_simulator",
                executable="fake_microphone_sender",
                output="screen",
                parameters=[
                    {
                        "wav_file": LaunchConfiguration("wav_file"),
                        "auto_play": ParameterValue(
                            LaunchConfiguration("auto_play"), value_type=bool
                        ),
                        "loop": ParameterValue(
                            LaunchConfiguration("loop"), value_type=bool
                        ),
                    }
                ],
            ),
        ]
    )
