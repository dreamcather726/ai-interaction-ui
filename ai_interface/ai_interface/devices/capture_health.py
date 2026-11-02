"""将真实录音进程状态通过已有 ComponentStatus 提供给可视化端。"""

from rclpy.clock import Clock, ClockType

from robot_interfaces.msg import ComponentStatus


class MicrophoneHealth:
    def __init__(self, node, get_process):
        self.node, self.get_process = node, get_process
        self.error_code, self.message = "", "麦克风节点待机；设备访问将在开始录音时检查"
        self.publisher = node.create_publisher(
            ComponentStatus, "/robot/component_status", 20
        )
        self.clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = node.create_timer(0.5, self.publish, clock=self.clock)

    def response(self, response):
        self.message = response.message
        self.error_code = "" if response.success else response.message.split(":", 1)[0]
        self.publish()
        return response

    def publish(self):
        process = self.get_process()
        running = process is not None and process.poll() is None
        message = ComponentStatus()
        message.stamp = self.node.get_clock().now().to_msg()
        message.component_id = "microphone"
        message.source_node = self.node.get_fully_qualified_name()
        message.enabled = True
        message.state = (
            message.BUSY
            if running
            else message.FAULT if self.error_code else message.READY
        )
        message.error_code = self.error_code
        message.message = (
            f"录音进程运行中，最多采集 {self.node.get_parameter('max_recording_seconds').value:g} 秒"
            if running
            else self.message
        )
        self.publisher.publish(message)
