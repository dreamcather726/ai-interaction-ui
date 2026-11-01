
"""接收 ROS2 音频数据，并保存为 WAV 文件"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import wave

import rclpy
from rclpy.node import Node

from robot_interfaces.msg import AudioSegment, RequestStatus
from ai_interface.audio_pipeline import AudioPipeline


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

        self.pipeline = AudioPipeline(self)
        self.get_logger().info('音频接收节点已启动：保存后自动请求 ASR，再进行语义解析')

    # 检查音频数据
    def validate_audio(self, msg):

        if msg.encoding != 'pcm_s16le':
            raise ValueError('音频格式错误')

        if msg.sample_rate != 16000:
            raise ValueError('采样率必须为16000Hz')

        if msg.channels != 1:
            raise ValueError('必须使用单声道')

        pcm = bytes(msg.data)

        if len(pcm) == 0:
            raise ValueError('音频数据为空')

        if len(pcm) > 320000:
            raise ValueError('音频长度超过10秒限制')

        if len(pcm) % 2 != 0:
            raise ValueError('PCM数据长度错误')

        if not all(value.strip() for value in (
                msg.request_id, msg.segment_id, msg.session_id, msg.input_context_id)):
            raise ValueError('音频请求、片段、会话和上下文ID不能为空')
        stamps = (msg.capture_started_at, msg.capture_ended_at, msg.stamp)
        if any(t.sec < 0 or not 0 <= t.nanosec < 10**9 for t in stamps):
            raise ValueError('音频时间戳非法')
        start, end, sent = (t.sec * 10**9 + t.nanosec for t in stamps)
        if not 0 < start < end <= sent or end - start > 10**10:
            raise ValueError('录音起止时间无效或超过10秒')
        if abs((end - start) / 10**9 - len(pcm) / 32000) > 0.05:
            raise ValueError('录音时长与音频帧数不一致')

        return pcm

    # 保存 PCM 为 WAV
    def save_wav(self, pcm):

        self.output_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        # 使用接收保存时的北京时间，与 WSL 系统时区无关。
        saved_at = datetime.now(timezone(timedelta(hours=8)))
        timestamp = saved_at.strftime('%Y%m%d_%H%M%S_%f')
        filename = f'audio_{timestamp}_{uuid4().hex}.wav'
        file_path = self.output_dir / filename

        with wave.open(str(file_path), 'wb') as wav:

            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)

            wav.writeframes(pcm)

        return file_path

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
            self.pipeline.enqueue(msg, file_path)


def main(args=None):

    rclpy.init(args=args)

    node = AudioReceiverNode()

    try:
        rclpy.spin(node)

    except KeyboardInterrupt:
        pass

    node.pipeline.close()
    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == '__main__':
    main()
