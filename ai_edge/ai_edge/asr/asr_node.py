"""提供文件语音识别服务；模型推理在工作线程执行，不阻塞 ROS 回调。"""

from ai_edge.common import error_codes as Codes

import sys
import time
import traceback
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.task import Future

from ai_edge.asr.asr_audio import read_asr_request
from ai_edge.asr.asr_health import ASRHealth
from ai_edge.asr.funasr_backend import ASRError, FunASRBackend
from ai_edge.asr.model_paths import default_model_path
from robot_interfaces.msg import RecognizedText
from robot_interfaces.srv import RecognizeAudio


class ASRNode(Node):
    """单模型、单推理任务；并发请求明确拒绝，不让模型调用无限排队。"""

    def __init__(self):
        super().__init__("asr_node")
        for name, value in (
            ("audio_root", "received_audio"),
            ("model_path", str(default_model_path())),
            ("device", "cpu"),
            ("language", "zh"),
            ("ncpu", 4),
            ("recognition_timeout", 180.0),
        ):
            self.declare_parameter(name, value)
        self.audio_root = (
            Path(self.get_parameter("audio_root").value).expanduser().resolve()
        )
        self.timeout = self.get_parameter("recognition_timeout").value
        if not 0 < self.timeout <= 300 or self.get_parameter("ncpu").value < 1:
            raise ValueError("recognition_timeout 必须在 (0,300] 秒，ncpu 必须大于零")
        self.backend = FunASRBackend(
            self.get_parameter("model_path").value,
            self.get_parameter("device").value,
            self.get_parameter("language").value,
            self.get_parameter("ncpu").value,
            progress=self.get_logger().info,
        )
        self.ready = False
        self.load_error = None
        self.pending = None
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="funasr")
        self.load_started = self.last_load_log = time.monotonic()
        self.get_logger().info(
            f"ASR 启动：Python={sys.executable}；音频目录={self.audio_root}；"
            f"模型={self.backend.model_name}；device={self.backend.device}"
        )
        self.loading = self.worker.submit(self.backend.load)
        self.health = ASRHealth(self)
        self.clock = Clock(clock_type=ClockType.STEADY_TIME)
        self.poll_timer = self.create_timer(0.05, self.poll, clock=self.clock)
        self.health_timer = self.create_timer(
            1.0, self.publish_health, clock=self.clock
        )
        self.group = ReentrantCallbackGroup()
        self.service = self.create_service(
            RecognizeAudio,
            "/ai/recognize_audio",
            self.recognize,
            callback_group=self.group,
        )
        self.get_logger().info("ASR 模型正在后台加载，请等待“模型已就绪”再录音")

    def poll(self):
        """定时回收后台任务；模型初始化与单次识别分别检查。"""
        self._poll_model_loading()
        self._poll_recognition()

    def _poll_model_loading(self):
        """初始化 Future 完成后才设置 ready；异常转为故障并保留堆栈。"""
        if self.loading is not None and self.loading.done():
            try:
                self.loading.result()
                self.ready = True
                self.get_logger().info(
                    f"FunASR 模型已就绪，初始化耗时 {time.monotonic() - self.load_started:.1f} 秒，"
                    "可以开始真实语音识别"
                )
            except Exception as exc:
                self.load_error = exc
                self.get_logger().error(
                    f"ASR 模型不可用：{exc}\n{traceback.format_exc()}"
                )
            self.loading = None
            self.publish_health()
        now = time.monotonic()
        if self.loading is not None and now - self.last_load_log >= 10:
            self.last_load_log = now
            self.get_logger().info(
                f"ASR 初始化仍在进行，已等待 {now - self.load_started:.0f} 秒；"
                "尚未就绪，请关注上方依赖导入与模型下载／加载输出"
            )

    def _poll_recognition(self):
        """回收推理结果或结束超时等待，不强行中断仍在运行的模型线程。"""
        if self.pending is None:
            return
        job = self.pending
        if time.monotonic() >= job["deadline"] and not job["response"].done():
            job["discard"] = True
            job["response"].set_exception(
                ASRError(Codes.ASR_TIMEOUT, "识别等待超时；本次不返回假结果")
            )
            self.publish_health()
        if job["worker"].done():
            if job.get("discard"):
                self.get_logger().warning(
                    "超时 ASR 推理已收尾，迟到结果已丢弃，可以受理新请求"
                )
                job["worker"].exception()
            elif not job["response"].done():
                try:
                    job["response"].set_result(job["worker"].result())
                except Exception as exc:
                    job["response"].set_exception(exc)
            self.pending = None
            self.publish_health()

    async def recognize(self, request, response):
        """原样关联输入信息，异步等待真实识别结果并统一封装失败响应。"""
        result = response.result
        started = time.monotonic()
        for field in (
            "request_id",
            "session_id",
            "segment_id",
            "input_context_id",
            "capture_started_at",
            "capture_ended_at",
        ):
            setattr(result, field, getattr(request, field))
        result.status = RecognizedText.FAILED
        try:
            _, pcm = read_asr_request(request, self.audio_root)
            if not self.ready:
                if self.load_error:
                    raise ASRError(
                        getattr(self.load_error, "code", Codes.ASR_MODEL_UNAVAILABLE),
                        str(self.load_error),
                    )
                raise ASRError(Codes.ASR_NOT_READY, "模型仍在初始化，请等待就绪提示")
            if self.pending is not None:
                raise ASRError(Codes.ASR_BUSY, "模型仍在处理上一条音频，本次未受理")
            answer = Future()
            self.pending = {
                "worker": self.worker.submit(self.backend.transcribe, pcm),
                "response": answer,
                "deadline": time.monotonic() + self.timeout,
                "discard": False,
            }
            self.get_logger().info(
                f"开始识别，请求 ID：{request.request_id}；文件={request.audio_path}；"
                f"音频 {len(pcm) / 32000:.2f} 秒；等待上限 {self.timeout:.1f} 秒"
            )
            transcript = await answer
            result.text = transcript.text
            result.language = transcript.language
            result.confidence = transcript.confidence
            if transcript.text:
                result.status, result.success = RecognizedText.SUCCESS, True
                result.message = "语音识别完成"
            else:
                result.status = RecognizedText.NO_SPEECH
                result.error_code, result.message = Codes.NO_SPEECH, "未识别到有效语音"
        except FileNotFoundError as exc:
            result.error_code, result.message = (
                Codes.FILE_NOT_FOUND,
                f"音频文件不存在：{exc}",
            )
        except ASRError as exc:
            result.error_code, result.message = exc.code, str(exc)
        except (ValueError, OSError, wave.Error, EOFError) as exc:
            result.error_code, result.message = (
                Codes.AUDIO_FORMAT_UNSUPPORTED,
                f"音频校验失败：{exc}",
            )
        except Exception as exc:
            result.error_code, result.message = Codes.ASR_FAILED, f"语音识别异常：{exc}"
        result.recognized_at = self.get_clock().now().to_msg()
        result.stamp = self.get_clock().now().to_msg()
        if result.success:
            self.get_logger().info(
                f"识别完成，请求 ID：{request.request_id}，"
                f"耗时 {time.monotonic() - started:.2f} 秒，文字：{result.text}"
            )
        else:
            self.get_logger().warning(
                f"识别未成功，请求 ID：{request.request_id}，"
                f"耗时 {time.monotonic() - started:.2f} 秒："
                f"{result.error_code}，{result.message}"
            )
        return response

    def publish_health(self):
        self.health.publish(
            self.ready,
            self.pending is not None,
            self.load_error,
            bool(self.pending and self.pending.get("discard")),
        )

    def close(self):
        """拒绝新的后台任务；Python 无法强行终止已经运行的推理线程。"""
        self.worker.shutdown(wait=False, cancel_futures=True)


def main(args=None):
    """启动文件 ASR 服务；使用已安装 FunASR 的 Python 解释器。"""
    rclpy.init(args=args)
    node = None
    try:
        node = ASRNode()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.close()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
