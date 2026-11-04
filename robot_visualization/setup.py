from setuptools import find_packages, setup
from glob import glob

package_name = "robot_visualization"

setup(
    name=package_name,
    version="0.0.0",
    packages=find_packages(),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("share/" + package_name + "/config", glob("config/*.yaml")),
    ],
    install_requires=["setuptools"],
    extras_require={"gui": ["PySide6==6.8.3"]},
    zip_safe=True,
    maintainer="tao",
    maintainer_email="tao@todo.todo",
    description="ROS 2 voice interaction dashboard and backend supervisor",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "dashboard = robot_visualization.app:main",
        ],
    },
)
