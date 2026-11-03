from glob import glob

from setuptools import find_packages, setup

setup(
    name="robot_simulator",
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/robot_simulator"]),
        ("share/robot_simulator", ["package.xml", "README.md"]),
        ("share/robot_simulator/launch", glob("launch/*.launch.py")),
        ("share/robot_simulator/test_data", glob("test_data/*")),
    ],
    install_requires=["setuptools"],
    tests_require=["pytest"],
    zip_safe=True,
    maintainer="hyt",
    maintainer_email="1254733009@qq.com",
    description="Hardware-free ROS 2 dashboard simulator",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "map_simulator_node = robot_simulator.map_simulator_node:main",
            "camera_simulator_node = robot_simulator.camera_simulator_node:main",
            "robot_state_simulator_node = robot_simulator.robot_state_simulator_node:main",
            "execute_task_action_server = robot_simulator.execute_task_action_server:main",
            "play_audio_action_server = robot_simulator.play_audio_action_server:main",
            "fake_microphone_sender = robot_simulator.fake_microphone_sender:main",
        ],
    },
)
