"""Replay a real WAV using the existing recording services and AudioSegment contract."""

from pathlib import Path
import time
from uuid import uuid4
import wave

from rclpy.node import Node
from robot_interfaces.msg import AudioSegment, ComponentStatus
from std_srvs.srv import Trigger

from robot_simulator.common import run_node, set_time


class FakeMicrophone(Node):
    def __init__(self):
        super().__init__("fake_microphone_sender")
        self.declare_parameter("wav_file", "")
        self.declare_parameter("auto_play", True)
        self.declare_parameter("loop", True)
        self.declare_parameter("loop_pause", 3.0)
        path = Path(self.get_parameter("wav_file").value).expanduser()
        if not path.is_file():
            raise ValueError(
                "请用 wav_file 参数指定现有的 16kHz、单声道、16bit PCM WAV 文件"
            )
        with wave.open(str(path), "rb") as source:
            if (
                source.getframerate(),
                source.getnchannels(),
                source.getsampwidth(),
                source.getcomptype(),
            ) != (16000, 1, 2, "NONE"):
                raise ValueError("WAV 必须为 16kHz、单声道、16bit PCM，不会擅自重采样")
            if not 0 < source.getnframes() <= 16000 * 300:
                raise ValueError("WAV 必须非空且不超过 300 秒")
            self.pcm = source.readframes(source.getnframes())
            if len(self.pcm) != source.getnframes() * 2:
                raise ValueError("WAV 数据不完整，实际 PCM 长度与文件头不一致")
        self.pause = self.get_parameter("loop_pause").value
        if self.pause < 0.5:
            raise ValueError("loop_pause 不得小于 0.5 秒")
        self.publisher = self.create_publisher(AudioSegment, "/audio/segments", 10)
        self.health_pub = self.create_publisher(
            ComponentStatus, "/robot/component_status", 20
        )
        self.start_service = self.create_service(
            Trigger, "/audio/start_recording", self.start_recording
        )
        self.stop_service = self.create_service(
            Trigger, "/audio/stop_recording", self.stop_recording
        )
        self.sim_start_service = self.create_service(
            Trigger, "/simulator/audio/start_recording", self.start_recording
        )
        self.sim_stop_service = self.create_service(
            Trigger, "/simulator/audio/stop_recording", self.stop_recording
        )
        self.session_id, self.context_id = (
            "sim-session-" + uuid4().hex,
            "sim-context-" + uuid4().hex,
        )
        self.auto_play = self.get_parameter("auto_play").value
        self.loop = self.get_parameter("loop").value
        self.recording = False
        self.cursor = 0
        self.next_start = time.monotonic() + 2.0
        self.message = "WAV 模拟麦克风待机；不访问真实麦克风"
        self.error_code = ""
        self.timer = self.create_timer(0.5, self.tick)
        self.get_logger().info(
            f"[SIM] WAV 回放就绪：{path}；每 0.5 秒采集一块，结束时发送完整片段"
        )

    def start_recording(self, request, response):
        if self.recording:
            response.message = "已有 WAV 模拟录音正在进行"
        elif self.publisher.get_subscription_count() == 0:
            response.message = "尚未发现 /audio/segments 接收者，请等待音频接收节点"
        else:
            self.recording, self.cursor, self.error_code = True, 0, ""
            response.success = True
            response.message = "模拟录音已开始：读取预录 WAV，不使用真实麦克风"
        self.message = response.message
        self.publish_health()
        return response

    def stop_recording(self, request, response):
        self.auto_play = False
        if not self.recording:
            response.success, response.message = True, "模拟录音已停止，不重复发送"
        elif self.cursor == 0:
            self.recording = False
            response.success, response.message = (
                False,
                "尚未采集到 0.5 秒数据；本次不发送空录音",
            )
        else:
            response.success = self.send_segment()
            response.message = self.message
        self.publish_health()
        return response

    def send_segment(self):
        self.recording = False
        self.next_start = time.monotonic() + self.pause
        if not self.loop:
            self.auto_play = False
        if self.publisher.get_subscription_count() == 0:
            self.error_code, self.message = (
                "AUDIO_RECEIVER_UNAVAILABLE",
                "音频接收者已离线，本次模拟音频未发送",
            )
            return False
        now = self.get_clock().now().nanoseconds
        if now <= self.cursor // 2 * 62500:
            self.error_code, self.message = (
                "INVALID_AUDIO_TIME",
                "ROS 时钟未就绪，本次模拟音频未发送",
            )
            return False
        message = AudioSegment()
        message.request_id, message.segment_id = (
            "sim-request-" + uuid4().hex,
            "sim-segment-" + uuid4().hex,
        )
        message.session_id, message.input_context_id = self.session_id, self.context_id
        set_time(message.stamp, now)
        set_time(message.capture_ended_at, now)
        set_time(message.capture_started_at, now - self.cursor // 2 * 62500)
        message.sample_rate, message.channels, message.encoding = 16000, 1, "pcm_s16le"
        message.data = self.pcm[: self.cursor]
        self.publisher.publish(message)
        self.error_code = ""
        self.message = f"模拟音频已发送：{self.cursor} 字节；请求 {message.request_id}"
        self.get_logger().info(self.message)
        return True

    def tick(self):
        if (
            not self.recording
            and self.auto_play
            and time.monotonic() >= self.next_start
            and self.publisher.get_subscription_count()
        ):
            self.start_recording(None, Trigger.Response())
        elif self.recording:
            self.cursor = min(
                len(self.pcm), self.cursor + 16000
            )  # 0.5 seconds of 16-bit mono PCM.
            if self.cursor == len(self.pcm):
                self.send_segment()
        self.publish_health()

    def publish_health(self):
        message = ComponentStatus()
        message.stamp = self.get_clock().now().to_msg()
        message.source_node, message.component_id = (
            self.get_fully_qualified_name(),
            "microphone",
        )
        message.enabled = True
        message.state = (
            message.BUSY
            if self.recording
            else message.FAULT if self.error_code else message.READY
        )
        message.message, message.error_code = self.message, self.error_code
        self.health_pub.publish(message)


def main(args=None):
    run_node(FakeMicrophone, args)
