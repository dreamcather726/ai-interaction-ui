"""接收 ROS2 音频数据，并保存为 WAV 文件"""

import wave
from pathlib import Path

import rclpy
from rclpy.node import Node

from ai_interface.audio.audio_forwarder import AudioForwarder
from ai_interface.audio.audio_storage import save_wav, validate_audio_segment
from robot_interfaces.msg import AudioSegment, RequestStatus


class AudioReceiverNode(Node):
    """接口端音频接收节点：接收、保存、转交，不执行语音识别。"""

    def __init__(self):
        super().__init__("audio_receiver_node")

        # WAV 文件保存位置
        self.declare_parameter("output_dir", "received_audio")
        self.output_dir = (
            Path(self.get_parameter("output_dir").value).expanduser().resolve()
        )

        # 发布接收结果
        self.receipt_pub = self.create_publisher(
            RequestStatus, "/ai/request_status", 10
        )

        # 接收 OrangePi 发布的音频
        self.audio_sub = self.create_subscription(
            AudioSegment, "/audio/segments", self.receive_audio, 10
        )

        self.forwarder = AudioForwarder(self)
        self.get_logger().info("音频接收节点已启动：保存后打包转交 ai_edge")

    def validate_audio(self, msg):
        """委托存储模块校验音频传输数据。"""
        return validate_audio_segment(msg)

    def save_wav(self, pcm):
        """委托存储模块保存 WAV，返回实际文件路径。"""
        return save_wav(self.output_dir, pcm)

    def receive_audio(self, msg):
        """处理一个音频片段，保存成功后提交 AI 端受理。"""
        result = RequestStatus()
        result.request_id = msg.request_id
        result.source_node = self.get_name()
        result.operation = "/audio/segments"

        try:
            # 1. 检查音频
            pcm = self.validate_audio(msg)

            # 2. 保存 WAV
            file_path = self.save_wav(pcm)

            # 3. 返回成功结果
            result.success = True
            result.stage = RequestStatus.RECEIVED
            result.message = "音频接收成功: " + str(file_path)

            self.get_logger().info(result.message)

        except (ValueError, OSError, wave.Error) as e:
            # 返回失败结果
            result.success = False
            result.stage = RequestStatus.REJECTED
            result.error_code = "AUDIO_RECEIVE_FAILED"
            result.message = str(e)

            self.get_logger().error(result.message)

        # 4. 发布接收回执
        result.stamp = self.get_clock().now().to_msg()
        self.receipt_pub.publish(result)
        if result.success:
            self.forwarder.enqueue(msg, file_path)


def main(args=None):
    """启动接口端接收节点，并在退出时清理转交请求。"""
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


if __name__ == "__main__":
    main()
