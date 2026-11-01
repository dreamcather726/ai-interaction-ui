"""音频任务入队、容量限制和重复输入检测，不依赖 ROS 节点。"""

import hashlib
import json
from collections import deque


class AudioJobQueue:
    """管理待处理音频和进程内去重记录，不参与模型推理。"""

    def __init__(self):
        self.queue = deque()
        self.seen_requests = {}
        self.seen_inputs = {}

    def __bool__(self):
        return bool(self.queue)

    def __len__(self):
        return len(self.queue)

    def popleft(self):
        """取出最早提交的任务，保持先进先出顺序。"""
        return self.queue.popleft()

    def clear(self):
        """清空待处理任务，但保留当前进程内的输入登记。"""
        self.queue.clear()

    def enqueue(self, msg, path, pcm, active=False):
        """保留来源上下文，在调用 AI 前检查重复输入及身份冲突。"""
        job = {"msg": msg, "path": path.resolve()}
        # 对字段值和音频内容计算指纹，避免 CDR 填充字节变化影响去重。
        metadata = [
            getattr(msg, field)
            for field in (
                "request_id",
                "session_id",
                "segment_id",
                "input_context_id",
                "sample_rate",
                "channels",
                "encoding",
            )
        ]
        metadata.extend(
            (getattr(msg, field).sec, getattr(msg, field).nanosec)
            for field in ("stamp", "capture_started_at", "capture_ended_at")
        )
        digest = hashlib.sha256(json.dumps(metadata).encode("utf-8"))
        digest.update(pcm)
        fingerprint = digest.hexdigest()
        key = (msg.session_id, msg.segment_id)
        for cache, identity, code in (
            (self.seen_requests, msg.request_id, "REQUEST_ID_CONFLICT"),
            (self.seen_inputs, key, "INPUT_ID_CONFLICT"),
        ):
            if identity in cache:
                same = cache[identity] == fingerprint
                return (
                    same,
                    "" if same else code,
                    (
                        "重复输入：任务已经登记，不重复提交识别"
                        if same
                        else "同一输入标识对应了不同内容，拒绝本次提交"
                    ),
                )
        if len(self.queue) + bool(active) >= 10:
            return False, "PIPELINE_BUSY", "音频已保存，但 AI 处理队列已满"
        if len(self.seen_requests) >= 1024:
            return False, "RECOVERY_REQUIRED", "音频已保存，但输入去重缓存已满"
        self.seen_requests[msg.request_id] = fingerprint
        self.seen_inputs[key] = fingerprint
        self.queue.append(job)
        return True, "", "AI 端已受理音频，任务已进入处理队列"
