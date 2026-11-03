"""Simulated PCM playback; never opens an audio device or synthesizes speech."""

import threading
import time

from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node
from robot_interfaces.action import PlayAudio

from robot_simulator.common import run_node, set_time


class PlayAudioSimulator(Node):
    def __init__(self):
        super().__init__("play_audio_action_server")
        self.lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.busy = False
        self.group = ReentrantCallbackGroup()
        self.server = ActionServer(
            self,
            PlayAudio,
            "/audio/play",
            execute_callback=self.execute,
            goal_callback=self.accept,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=self.group,
        )
        self.get_logger().info("[SIM] /audio/play 已就绪；仅等待 1 秒，不访问音频设备")

    def accept(self, request):
        with self.lock:
            if self.busy or (
                not request.request_id.strip()
                or request.sample_rate != 16000
                or request.channels != 1
                or request.encoding != "pcm_s16le"
                or not request.data
                or len(request.data) > 320000
                or len(request.data) % 2
            ):
                return GoalResponse.REJECT
            self.busy = True
            return GoalResponse.ACCEPT

    def execute(self, handle):
        request = handle.request
        result = PlayAudio.Result()
        result.request_id = request.request_id
        total = len(request.data) // 2
        started, last = time.monotonic(), -1.0
        try:
            while True:
                elapsed = min(1.0, time.monotonic() - started)
                result.played_frames = min(total, int(total * elapsed))
                if not self.context.ok() or self.shutdown_event.is_set():
                    result.error_code, result.message = (
                        "SIMULATOR_STOPPED",
                        "模拟播报服务已关闭",
                    )
                    if self.context.ok():
                        handle.abort()
                    break
                if handle.is_cancel_requested:
                    result.error_code, result.message = "CANCELED", "模拟播报已取消"
                    handle.canceled()
                    break
                if elapsed >= 1.0:
                    result.success, result.message = True, "模拟播报完成"
                    result.played_frames = total
                    handle.succeed()
                    break
                if elapsed - last >= 0.2:
                    feedback = PlayAudio.Feedback()
                    feedback.stamp = self.get_clock().now().to_msg()
                    feedback.request_id = request.request_id
                    feedback.stage = feedback.PLAYING
                    feedback.message = "仅模拟播报，不输出声音"
                    feedback.played_frames, feedback.total_frames = (
                        result.played_frames,
                        total,
                    )
                    handle.publish_feedback(feedback)
                    last = elapsed
                self.shutdown_event.wait(0.02)
            result.stamp = self.get_clock().now().to_msg()
            set_time(
                result.played_duration,
                result.played_frames * 10**9 // request.sample_rate,
            )
            return result
        finally:
            with self.lock:
                self.busy = False

    def destroy_node(self):
        self.server.destroy()
        return super().destroy_node()


def main(args=None):
    run_node(PlayAudioSimulator, args, threaded=True)
