"""Shared launch assembly: common < deployment < local < explicit arguments."""

from robot_visualization.config import parameter_names
from robot_visualization.configuration import (
    load_configuration,
    parse_override,
    read_parameter_file,  # Compatibility for existing callers.
)


def dashboard_actions(context):
    from launch.actions import LogInfo
    from launch.substitutions import LaunchConfiguration
    from launch_ros.actions import Node
    from launch_ros.parameter_descriptions import ParameterValue

    files = []
    for argument in ("common_params_file", "params_file", "local_params_file"):
        filename = LaunchConfiguration(argument).perform(context).strip()
        if not filename and argument == "local_params_file":
            continue
        files.append(filename)
    overrides = {}
    for name in sorted(parameter_names()):
        value = LaunchConfiguration(name).perform(context).strip()
        if value:
            overrides[name] = parse_override(name, value)
    values = load_configuration(files, overrides)
    parameters = {
        name: (
            value
            if isinstance(value, list)
            else ParameterValue(value, value_type=type(value))
        )
        for name, value in values.items()
        if value != []
    }
    return [
        LogInfo(msg="Dashboard config: " + values["config_sources"]),
        Node(
            package="robot_visualization",
            executable="dashboard",
            output="screen",
            prefix="/usr/bin/python3",
            parameters=[parameters],
            additional_env={"ROS_DOMAIN_ID": LaunchConfiguration("domain_id")},
        ),
    ]


def dashboard_launch(mode, domain_id):
    from launch import LaunchDescription
    from launch.actions import DeclareLaunchArgument, OpaqueFunction
    from launch.substitutions import PathJoinSubstitution
    from launch_ros.substitutions import FindPackageShare

    if mode not in ("simulation", "real"):
        raise ValueError(f"unknown deployment mode: {mode}")

    def installed(name):
        return PathJoinSubstitution(
            [FindPackageShare("robot_visualization"), "config", name]
        )

    return LaunchDescription(
        [
            DeclareLaunchArgument("domain_id", default_value=str(domain_id)),
            DeclareLaunchArgument(
                "common_params_file", default_value=installed("common.yaml")
            ),
            DeclareLaunchArgument(
                "params_file", default_value=installed(mode + ".yaml")
            ),
            DeclareLaunchArgument("local_params_file", default_value=""),
            *[
                DeclareLaunchArgument(name, default_value="")
                for name in sorted(parameter_names())
            ],
            OpaqueFunction(function=dashboard_actions),
        ]
    )
