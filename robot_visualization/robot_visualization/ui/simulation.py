"""Compatibility imports; robot presentation lives in ui.robot for both modes."""

from robot_visualization.ui.robot import (
    RobotImage,
    RobotView,
    SimulationImage,
    SimulationView,
)

__all__ = ["RobotImage", "RobotView", "SimulationImage", "SimulationView"]
