
"""接收 ROS2 音频数据，并保存为 WAV 文件"""

from pathlib import Path
import wave

import rclpy
from rclpy.node import Node

from robot_interfaces.msg import AudioSegment, RequestStatus
from ai_interface.audio_forwarder import AudioForwarder
from ai_interface.audio_storage import validate_audio_segment, save_wav


class AudioReceiverNode(Node):

    def __init__(self):
        super().__init__('audio_receiver_node')

        # WAV 文件保存位置
        self.declare_parameter('output_dir', 'received_audio')
        self.output_dir = Path(
            self.get_parameter('output_dir').value
        ).expanduser().resolve()

        # 发布接收结果
        self.receipt_pub = self.create_publisher(
            RequestStatus,
            '/ai/request_status',
            10
        )

        # 接收 OrangePi 发布的音频
        self.audio_sub = self.create_subscription(
            AudioSegment,
            '/audio/segments',
            self.receive_audio,
            10
        )

        self.forwarder = AudioForwarder(self)
        self.get_logger().info('音频接收节点已启动：保存后打包转交 ai_edge')

    # 检查音频数据
    def validate_audio(self, msg):
        return validate_audio_segment(msg)
    # 保存 PCM 为 WAV
    def save_wav(self, pcm):
        return save_wav(self.output_dir, pcm)
    # 接收音频
    def receive_audio(self, msg):

        result = RequestStatus()

        result.request_id = msg.request_id
        result.source_node = self.get_name()
        result.operation = '/audio/segments'

        try:

            # 1. 检查音频
            pcm = self.validate_audio(msg)

            # 2. 保存 WAV
            file_path = self.save_wav(pcm)

            # 3. 返回成功结果
            result.success = True
            result.stage = RequestStatus.RECEIVED
            result.message = '音频接收成功: ' + str(file_path)

            self.get_logger().info(result.message)

        except (ValueError, OSError, wave.Error) as e:

            # 返回失败结果
            result.success = False
            result.stage = RequestStatus.REJECTED
            result.error_code = 'AUDIO_RECEIVE_FAILED'
            result.message = str(e)

            self.get_logger().error(result.message)

        # 4. 发布接收回执
        result.stamp = self.get_clock().now().to_msg()

        self.receipt_pub.publish(result)
        if result.success:
            self.forwarder.enqueue(msg, file_path)


def main(args=None):

    rclpy.init(args=args)

    node = AudioReceiverNode()

    try:
        rclpy.spin(node)

    except KeyboardInterrupt:
        pass

    node.forwarder.close()
    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == '__main__':
    main()
