"""保存有期限的待澄清候选；不调用模型，不生成执行授权。"""

import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass

from ai_edge.semantic.semantic_contract import SemanticError, validate_candidate


@dataclass
class PendingDialogue:
    candidate: dict
    context: list
    missing_fields: tuple
    prompted_at_ns: int
    expires_at: float


def dialogue_command(text):
    """仅识别整句控制词，不把“取消返回”等任务内容当成取消对话。"""
    command = text.strip().rstrip("。！？.!?，,；; ")
    if command in {"取消", "取消任务", "取消当前任务", "不用了"}:
        return "cancel"
    if command in {"重新开始", "新任务", "开始新任务"}:
        return "reset"
    return ""


def bounded_context(turns):
    """保留最近对话，完整候选另存；不截断一句话后改变其含义。"""
    kept, size = [], 0
    for turn in reversed(turns):
        length = len(turn["text"].encode("utf-8"))
        if len(kept) >= 6 or size + length > 800:
            break
        kept.append(dict(turn))
        size += length
    return list(reversed(kept))


class DialogueStore:
    """同一会话及上下文可续轮；过期、重启后不恢复旧候选。"""

    def __init__(self, timeout=300.0, limit=32, clock=time.monotonic):
        if not 0 < timeout <= 1800 or limit < 1:
            raise ValueError("澄清等待时间必须在 (0,1800] 秒内，容量必须为正数")
        self.timeout, self.limit, self.clock = timeout, limit, clock
        self.pending = OrderedDict()

    # 续轮读取与上下文边界检查。

    def get(self, key, captured_at_ns, stations, objects):
        """提问前已开始的录音不能自动作为该问题的回答。"""
        pending = self.pending.get(key)
        if pending is None:
            return None

        if self.clock() >= pending.expires_at:
            self.forget(key)
            raise SemanticError(
                "DIALOGUE_EXPIRED", "补充等待已超时，请重新描述完整任务"
            )
        if captured_at_ns < pending.prompted_at_ns:
            raise SemanticError(
                "CLARIFICATION_NOT_READY",
                "这段录音开始于追问之前，请看到追问后重新录音回答",
            )

        try:
            validate_candidate(pending.candidate, stations, objects)
        except SemanticError as exc:
            self.forget(key)
            raise SemanticError(
                "DIALOGUE_CONFIG_CHANGED", "已有候选不再符合当前白名单，请重新描述任务"
            ) from exc
        return deepcopy(pending)

    # 待补充候选与最近对话的保存。

    def remember(self, key, text, answer, prompted_at_ns, previous=None):
        """只保存规则校验后仍缺字段的候选，拒绝静默淘汰其他会话。"""
        if answer.unsupported or not answer.missing_fields:
            raise ValueError("只能保存待澄清候选")

        now = self.clock()
        for old_key, item in list(self.pending.items()):
            if old_key != key and item.expires_at <= now:
                self.forget(old_key)
        if key not in self.pending and len(self.pending) >= self.limit:
            raise SemanticError("DIALOGUE_BUSY", "待补充会话已满，请稍后重新描述任务")

        context = list(previous.context) if previous else []
        context.extend(
            [
                {"role": "user", "text": text},
                {"role": "assistant", "text": answer.clarification_question},
            ]
        )
        self.pending[key] = PendingDialogue(
            dict(answer.candidate),
            bounded_context(context),
            tuple(answer.missing_fields),
            prompted_at_ns,
            now + self.timeout,
        )

    # 单个上下文与整个缓存的清理。

    def forget(self, key):
        return self.pending.pop(key, None)

    def clear(self):
        self.pending.clear()
