"""ASR 模型就绪状态与节点心跳，供可视化和监控组订阅。"""

from ai_edge.common import error_codes as Codes

import time
from uuid import uuid4

from robot_interfaces.msg import ComponentStatus, NodeHeartbeat


class ASRHealth:
    """复用已有部件状态和心跳类型，不参与识别或任务执行。"""

    def __init__(self, node):
        self.node = node
        self.boot_id = uuid4().hex
        self.sequence = 0
        self.started = time.monotonic()
        self.components = node.create_publisher(
            ComponentStatus, "/robot/component_status", 20
        )
        self.heartbeats = node.create_publisher(NodeHeartbeat, "/system/heartbeat", 50)

    def publish(self, ready, busy, load_error, draining=False):
        """发布模型能力与节点活性；心跳在线不等同于模型已经可用。"""
        status = ComponentStatus()
        status.stamp = self.node.get_clock().now().to_msg()
        status.source_node = self.node.get_fully_qualified_name()
        status.component_id = "asr"
        status.enabled = ready and not draining
        if load_error:
            status.state = status.FAULT
            status.error_code = getattr(load_error, "code", Codes.ASR_MODEL_UNAVAILABLE)
            status.message = str(load_error)
        elif not ready:
            status.state, status.message = status.STARTING, "ASR 模型加载中"
        elif draining:
            status.state, status.message = (
                status.DEGRADED,
                "ASR 超时收尾中；迟到结果丢弃，模型线程结束前不受理新请求",
            )
            status.error_code = Codes.ASR_TIMEOUT
        elif busy:
            status.state, status.message = status.BUSY, "ASR 正在处理音频"
        else:
            status.state, status.message = status.READY, "ASR 模型已就绪"
        self.components.publish(status)
        heartbeat = NodeHeartbeat()
        heartbeat.stamp = status.stamp
        heartbeat.node_name = status.source_node
        heartbeat.boot_id = self.boot_id
        self.sequence += 1
        heartbeat.sequence = self.sequence
        heartbeat.uptime.sec, heartbeat.uptime.nanosec = divmod(
            int((time.monotonic() - self.started) * 10**9), 10**9
        )
        heartbeat.expected_period.sec = 1
        heartbeat.state = (
            heartbeat.DEGRADED
            if load_error or draining
            else (heartbeat.RUNNING if ready else heartbeat.STARTING)
        )
        heartbeat.error_code, heartbeat.message = status.error_code, status.message
        self.heartbeats.publish(heartbeat)
