from setuptools import find_packages, setup

package_name = 'ai_edge'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    extras_require={
        'asr': ['funasr>=1.3.26,<2', 'numpy>=1.26,<2'],
    },
    zip_safe=True,
    maintainer='tao',
    maintainer_email='tao@todo.todo',
    description='TODO: Package description',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'audio_processing_node = ai_edge.audio_processing_node:main',
            'asr_node = ai_edge.asr_node:main',
        ],
    },
)
