"""FunASR 模型适配层：只负责模型加载和真实 PCM 转文字。"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ai_edge.common import error_codes as Codes
from ai_edge.asr.model_paths import default_model_path


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
        self,
        model=None,
        device="cpu",
        language="zh",
        ncpu=4,
        progress=None,
    ):
        self.model_name = str(default_model_path() if model is None else model)
        self.device = device
        self.language = language
        self.ncpu = ncpu
        self.model = None
        self.progress = progress or (lambda message: None)

    def load(self):
        """加载官方集成模型；权重缓存留在运行机，不进入 Git 仓库。"""
        model_dir = Path(self.model_name).expanduser()
        checkpoint = None
        if model_dir.is_dir():
            checkpoint = self._local_checkpoint(model_dir)
        elif model_dir.is_absolute():
            raise ASRError(
                Codes.ASR_MODEL_INCOMPLETE,
                f"本地模型目录不存在：{model_dir}。请将完整模型放入项目 models/SenseVoiceSmall",
            )
        self.progress("正在导入 FunASR 及其依赖（PyTorch、音频处理库等）……")
        try:
            from funasr import AutoModel
        except ImportError as exc:
            raise ASRError(
                Codes.ASR_DEPENDENCY_MISSING, f"当前 Python 无法导入 FunASR：{exc}"
            ) from exc
        try:
            source = "本地模型目录" if checkpoint is not None else "指定模型"
            self.progress(
                f"FunASR 依赖导入成功；正在初始化{source} {self.model_name}，"
                f"device={self.device}"
            )
            options = {}
            if checkpoint is not None:
                # 显式传入权重；部分 FunASR 版本缺少文件时仍会创建随机初始化模型。
                options["init_param"] = str(checkpoint)
            model = AutoModel(
                model=self.model_name,
                device=self.device,
                ncpu=self.ncpu,
                trust_remote_code=False,
                disable_update=True,
                disable_pbar=True,
                ignore_init_mismatch=False,
                **options,
            )
            if checkpoint is None:
                resolved = getattr(model, "kwargs", {}).get("init_param")
                if not resolved:
                    raise ASRError(
                        Codes.ASR_MODEL_INCOMPLETE,
                        "FunASR 没有提供已加载的预训练权重路径，模型不能报告就绪",
                    )
                checkpoint = Path(resolved)
            self._require_checkpoint(checkpoint)
            self.model = model
            self.progress(f"预训练权重已加载：{checkpoint}")
        except ASRError:
            raise
        except Exception as exc:
            raise ASRError(
                Codes.ASR_MODEL_UNAVAILABLE, f"FunASR 模型加载失败：{exc}"
            ) from exc

    def _local_checkpoint(self, model_dir):
        """根据官方清单查找权重；只有下载目录不代表模型已完整。"""
        checkpoint_name = "model.pt"
        manifest = model_dir / "configuration.json"
        if manifest.is_file():
            try:
                metadata = json.loads(manifest.read_text(encoding="utf-8"))
                checkpoint_name = metadata.get("file_path_metas", {}).get(
                    "init_param", checkpoint_name
                )
                if not isinstance(checkpoint_name, str) or not checkpoint_name:
                    raise ValueError("init_param 必须是非空权重路径")
            except (OSError, ValueError, AttributeError) as exc:
                raise ASRError(
                    Codes.ASR_MODEL_UNAVAILABLE, f"模型文件清单无效：{manifest}；{exc}"
                ) from exc
        checkpoint = model_dir / checkpoint_name
        self._require_checkpoint(checkpoint)
        return checkpoint

    @staticmethod
    def _require_checkpoint(checkpoint):
        if not checkpoint.is_file() or checkpoint.stat().st_size == 0:
            partial = checkpoint.with_name(checkpoint.name + ".incomplete")
            detail = f"；发现未完成下载：{partial}" if partial.exists() else ""
            raise ASRError(
                Codes.ASR_MODEL_INCOMPLETE,
                f"模型权重缺失或为空：{checkpoint}{detail}。请完成模型下载后重启 ASR",
            )

    def transcribe(self, pcm):
        """输入经校验的 PCM16；只去除模型标签，不改写识别出的文字。"""
        import numpy as np

        if self.model is None:
            raise ASRError(Codes.ASR_NOT_READY, "ASR 模型尚未加载完成")
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size == 0 or not np.any(samples):
            return Transcription("")
        try:
            results = []
            chunk_size = 25 * 16000
            for offset in range(0, samples.size, chunk_size):
                self.progress(
                    f"语音识别片段 {offset // chunk_size + 1}/{(samples.size + chunk_size - 1) // chunk_size}"
                )
                part = self.model.generate(
                    input=samples[offset : offset + chunk_size],
                    fs=16000,
                    cache={},
                    language=self.language,
                    use_itn=True,
                )
                if not isinstance(part, list):
                    raise ASRError(
                        Codes.INVALID_ASR_RESULT, "FunASR 返回的数据结构不正确"
                    )
                results.extend(part)
        except Exception as exc:
            raise ASRError(Codes.ASR_FAILED, f"FunASR 推理失败：{exc}") from exc
        if not isinstance(results, list):
            raise ASRError(Codes.INVALID_ASR_RESULT, "FunASR 返回的数据结构不正确")
        texts = []
        language = ""
        language_map = {"zh": "zh-CN", "en": "en", "yue": "yue", "ja": "ja", "ko": "ko"}
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ASRError(Codes.INVALID_ASR_RESULT, "FunASR 返回结果缺少有效 text")
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
        if len(text) > 16384:
            raise ASRError(
                Codes.ASR_OUTPUT_TOO_LONG,
                "识别文字超过 16384 字符，未截断或进入语义解析",
            )
        return Transcription(text, language or language_map.get(self.language, ""))
