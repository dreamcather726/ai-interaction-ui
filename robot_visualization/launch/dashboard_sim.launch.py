"""Dashboard deployment using layered ROS configuration."""

from robot_visualization.deployment import dashboard_launch


def generate_launch_description():
    return dashboard_launch("simulation", 71)
