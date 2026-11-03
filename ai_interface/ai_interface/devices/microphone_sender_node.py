"""使用 parecord 采集真实麦克风，并通过 ROS 2 发送 PCM 音频。"""

import os
import shutil
import signal
import subprocess
import tempfile
import time
from uuid import uuid4

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from std_srvs.srv import Trigger

from ai_interface.devices.capture_health import MicrophoneHealth
from ai_interface.devices import error_codes as Codes
from robot_interfaces.msg import AudioSegment


class MicrophoneSenderNode(Node):
    """提供真实麦克风的开始、停止和自动限时录音服务。"""

    def __init__(self):
        super().__init__("microphone_sender_node")
        self.declare_parameter("device", "@DEFAULT_SOURCE@")
        self.declare_parameter("max_recording_seconds", 120.0)
        self.declare_parameter("session_id", "mic-session-" + uuid4().hex)
        self.declare_parameter("input_context_id", "mic-context-" + uuid4().hex)
        self.device = self.get_parameter("device").value
        self.max_seconds = self.get_parameter("max_recording_seconds").value
        if not 1 <= self.max_seconds <= 300:
            raise ValueError("max_recording_seconds 必须在 1～300 秒之间")
        self.max_bytes = int(self.max_seconds * 16000) * 2
        # 录音子进程及 PCM 缓冲仅在一次采集期间有效。
        self.process = None
        self.errors = None
        self.pcm = bytearray()
        self.started_at = None
        self.session_id = ""
        self.context_id = ""
        self.health = MicrophoneHealth(self, lambda: self.process)
        self.publisher = self.create_publisher(AudioSegment, "/audio/segments", 10)
        self.start_service = self.create_service(
            Trigger, "/audio/start_recording", self.start_recording
        )
        self.stop_service = self.create_service(
            Trigger, "/audio/stop_recording", self.stop_recording
        )
        # 使用单调时钟，系统时间调整不会延长配置的采集上限。
        self.timer_clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.timer = self.create_timer(
            0.05, self.poll_recording, clock=self.timer_clock
        )
        self.get_logger().info(
            f"真实麦克风节点已就绪，等待录音请求；设备={self.device}"
        )

    def start_recording(self, request, response):
        """控制回执与实际采集状态同步发布。"""
        return self.health.response(self._start_recording(request, response))

    def _start_recording(self, request, response):
        """启动录音子进程，避免阻塞 ROS 回调执行器。"""
        if self.process is not None:
            response.success = self.process.poll() is None
            response.message = (
                "正在录音，无需重复开始"
                if response.success
                else (
                    f"{Codes.AUDIO_DEVICE_UNAVAILABLE}: 上一次采集已退出，请先调用停止服务清理"
                )
            )
            return response
        executable = shutil.which("parecord")
        if executable is None:
            response.message = f"{Codes.AUDIO_DEVICE_UNAVAILABLE}: 未找到 parecord，请安装 pulseaudio-utils"
            return response
        self.session_id = self.get_parameter("session_id").value.strip()
        self.context_id = self.get_parameter("input_context_id").value.strip()
        if not self.session_id or not self.context_id:
            response.message = (
                f"{Codes.INVALID_REQUEST}: 会话 ID 和输入上下文 ID 不能为空"
            )
            return response
        if self.get_clock().now().nanoseconds <= 0:
            response.message = f"{Codes.INVALID_REQUEST}: ROS 时钟尚未就绪"
            return response
        try:
            self.errors = tempfile.TemporaryFile()
            self.process = subprocess.Popen(
                [
                    executable,
                    "--raw",
                    "--format=s16le",
                    "--rate=16000",
                    "--channels=1",
                    "--latency-msec=50",
                    "--device=" + self.device,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=self.errors,
            )
            os.set_blocking(self.process.stdout.fileno(), False)
        except (OSError, ValueError) as exc:
            self.abort_recording()
            response.message = (
                f"{Codes.AUDIO_DEVICE_UNAVAILABLE}: 麦克风设备不可用：{exc}"
            )
            return response
        self.pcm.clear()
        self.started_at = time.monotonic()
        response.success = True
        response.message = "录音进程已启动，正在等待麦克风 PCM 数据"
        return response

    def drain_audio(self):
        """持续读取录音管道，避免缓冲区写满后阻塞采集。"""
        while self.process is not None:
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            remaining = self.max_bytes - len(self.pcm)
            self.pcm.extend(chunk[:remaining])
            if len(self.pcm) >= self.max_bytes:
                break

    def poll_recording(self):
        """收集 PCM 数据，到达配置的时间或字节上限后自动停止并发送。"""
        if self.process is None:
            return
        self.drain_audio()
        if (
            self.process.poll() is not None
            or len(self.pcm) >= self.max_bytes
            or time.monotonic() - self.started_at >= self.max_seconds
        ):
            result = self.health.response(self._do_stop_capture(Trigger.Response()))
            if result.success:
                self.get_logger().info(result.message)
            else:
                self.get_logger().error(result.message)

    def close_capture(self):
        """停止录音子进程，读取剩余数据并释放管道与临时资源。"""
        process = self.process
        if process is None:
            return b"", "", 0
        try:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                remaining, _ = process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=1)
                raise RuntimeError("录音进程未正常停止，已丢弃本次音频")
            self.errors.seek(0)
            error = self.errors.read(4096).decode(errors="replace").strip()
            return remaining or b"", error, process.returncode
        finally:
            if process.stdout is not None:
                process.stdout.close()
            self.errors.close()
            self.process = None
            self.errors = None
            self.started_at = None

    def stop_recording(self, request, response):
        """手动／自动停止共用状态广播，界面无需靠倒计时推断。"""
        return self.health.response(self._do_stop_capture(response))

    def _do_stop_capture(self, response):
        """停止实际采集，仅发送包含完整采样帧的有效 PCM 数据。"""
        if self.process is None:
            response.success = True
            response.message = "录音已停止，不重复发送旧音频"
            return response
        try:
            remaining, error, code = self.close_capture()
            self.pcm.extend(remaining[: self.max_bytes - len(self.pcm)])
            if code not in (0, -signal.SIGINT):
                raise RuntimeError(error or f"parecord 异常退出，退出码为 {code}")
            if not self.pcm or len(self.pcm) % 2:
                raise RuntimeError("未取得完整 PCM 音频，请检查麦克风访问权限")
            if self.publisher.get_subscription_count() == 0:
                raise RuntimeError("录音已停止，但未发现音频接收节点；本次数据已丢弃")
            now = self.get_clock().now().nanoseconds
            start = now - len(self.pcm) // 2 * 62500
            if start <= 0:
                raise RuntimeError("ROS 时钟尚未就绪，本次音频已丢弃")
            msg = AudioSegment()
            msg.request_id = "mic-request-" + uuid4().hex
            msg.segment_id = "mic-segment-" + uuid4().hex
            msg.session_id, msg.input_context_id = self.session_id, self.context_id
            msg.capture_started_at.sec, msg.capture_started_at.nanosec = divmod(
                start, 10**9
            )
            msg.capture_ended_at.sec, msg.capture_ended_at.nanosec = divmod(now, 10**9)
            msg.stamp.sec, msg.stamp.nanosec = divmod(now, 10**9)
            msg.sample_rate, msg.channels, msg.encoding = 16000, 1, "pcm_s16le"
            msg.data = bytes(self.pcm)
            self.publisher.publish(msg)
            response.success = True
            response.message = (
                f"音频已发送：{len(self.pcm)} 字节 PCM，请求 ID：{msg.request_id}"
            )
        except (OSError, RuntimeError) as exc:
            response.message = f"{Codes.AUDIO_CAPTURE_FAILED}: 音频采集失败：{exc}"
        finally:
            self.pcm.clear()
        return response

    def abort_recording(self):
        """退出时停止录音并丢弃未发送数据，清理过程不发布音频。"""
        try:
            self.close_capture()
        except (OSError, RuntimeError) as exc:
            self.get_logger().error(str(exc))
        finally:
            if self.errors is not None:
                self.errors.close()
                self.errors = None
            self.pcm.clear()


def main(args=None):
    """运行真实麦克风节点，并在退出时释放录音资源。"""
    rclpy.init(args=args)
    node = None
    try:
        node = MicrophoneSenderNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.abort_recording()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
