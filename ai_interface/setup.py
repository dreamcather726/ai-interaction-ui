from setuptools import find_packages, setup

package_name = "ai_interface"

setup(
    name=package_name,
    version="0.0.0",
    packages=find_packages(),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="tao",
    maintainer_email="tao@todo.todo",
    description="TODO: Package description",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "audio_receiver_node = ai_interface.audio.audio_receiver_node:main",
            "microphone_sender_node = ai_interface.devices.microphone_sender_node:main",
        ],
    },
)
