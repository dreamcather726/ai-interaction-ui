"""Describe the code and interface actually loaded by this process."""

import importlib.util
from pathlib import Path
from xml.etree import ElementTree


def loaded_runtime():
    paths = {}
    for package in ("robot_visualization", "ai_edge", "robot_interfaces"):
        spec = importlib.util.find_spec(package)
        paths[package] = (
            str(Path(spec.origin).resolve()) if spec and spec.origin else "unavailable"
        )
    report = {"code_paths": paths}
    try:
        from ament_index_python.packages import get_package_share_directory

        package_xml = (
            Path(get_package_share_directory("robot_interfaces")) / "package.xml"
        )
        report["interface_package_version"] = ElementTree.parse(package_xml).findtext(
            "version"
        )
        report["interface_package_path"] = str(package_xml.resolve())
    except (ImportError, LookupError, OSError, ElementTree.ParseError) as exc:
        report["interface_package_version"] = f"unavailable: {exc}"
    try:
        from robot_interfaces.msg import TaskSpec

        report["task_interface_version"] = TaskSpec().interface_version
    except ImportError:
        report["task_interface_version"] = "unavailable"
    return report
