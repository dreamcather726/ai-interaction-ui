"""FunASR 模型适配层：只负责模型加载和真实 PCM 转文字。"""

import re
from dataclasses import dataclass


class ASRError(RuntimeError):
    """把后端异常转换为稳定错误码，避免 ROS 层依赖模型实现细节。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Transcription:
    text: str
    language: str = ""
    confidence: float = -1.0


class FunASRBackend:
    """复用一次加载的 SenseVoice 模型，不在每个请求中重新加载权重。"""

    def __init__(
        self, model="iic/SenseVoiceSmall", device="cpu", language="zh", ncpu=4,
        progress=None,
    ):
        self.model_name = model
        self.device = device
        self.language = language
        self.ncpu = ncpu
        self.model = None
        self.progress = progress or (lambda message: None)

    def load(self):
        """加载官方集成模型；权重缓存留在运行机，不进入 Git 仓库。"""
        self.progress("正在导入 FunASR 及其依赖（PyTorch、音频处理库等）……")
        try:
            from funasr import AutoModel
        except ImportError as exc:
            raise ASRError(
                "ASR_DEPENDENCY_MISSING", f"当前 Python 无法导入 FunASR：{exc}"
            ) from exc
        try:
            self.progress(
                f"FunASR 依赖导入成功；正在初始化模型 {self.model_name}，"
                f"device={self.device}；缓存缺失时由模型库下载权重"
            )
            self.model = AutoModel(
                model=self.model_name,
                device=self.device,
                ncpu=self.ncpu,
                trust_remote_code=False,
                disable_update=True,
                disable_pbar=True,
            )
        except Exception as exc:
            raise ASRError(
                "ASR_MODEL_UNAVAILABLE", f"FunASR 模型加载失败：{exc}"
            ) from exc

    def transcribe(self, pcm):
        """输入经校验的 PCM16；只去除模型标签，不改写识别出的文字。"""
        import numpy as np

        if self.model is None:
            raise ASRError("ASR_NOT_READY", "ASR 模型尚未加载完成")
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size == 0 or not np.any(samples):
            return Transcription("")
        try:
            results = self.model.generate(
                input=samples, fs=16000, cache={}, language=self.language, use_itn=True
            )
        except Exception as exc:
            raise ASRError("ASR_FAILED", f"FunASR 推理失败：{exc}") from exc
        if not isinstance(results, list):
            raise ASRError("INVALID_ASR_RESULT", "FunASR 返回的数据结构不正确")
        texts = []
        language = ""
        language_map = {"zh": "zh-CN", "en": "en", "yue": "yue", "ja": "ja", "ko": "ko"}
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ASRError("INVALID_ASR_RESULT", "FunASR 返回结果缺少有效 text")
            raw = item["text"]
            if "<|nospeech|>" in raw:
                continue
            for token, code in language_map.items():
                if f"<|{token}|>" in raw:
                    language = code
                    break
            # SenseVoice 的语言、情绪、声音事件标签不是用户说出的任务文字。
            text = re.sub(r"<\|[^|]*\|>", "", raw).strip()
            if text:
                texts.append(text)
        text = " ".join(texts).strip()
        if len(text) > 4096:
            raise ASRError(
                "ASR_OUTPUT_TOO_LONG", "识别文字超过 4096 字符，未截断或进入语义解析"
            )
        return Transcription(text, language or language_map.get(self.language, ""))
