"""为可视化交互组发布音频阶段和类型化结果，并提供晚加入查询入口。"""

from copy import deepcopy
from uuid import uuid4

from robot_interfaces.msg import AudioProcessingState, TaskParseReport
from robot_interfaces.srv import QueryAudio


class AudioVisualization:
    """仅维护展示用的进程内快照，不控制模型、不授权任务执行。"""

    def __init__(self, node):
        self.node = node
        self.boot_id = uuid4().hex
        self.records = {}

        self.states = node.create_publisher(AudioProcessingState, "/ai/audio_state", 10)
        self.parses = node.create_publisher(TaskParseReport, "/ai/parse_result", 10)
        self.query_service = node.create_service(
            QueryAudio, "/ai/query_audio", self.query
        )

    # 阶段广播：关联来源、递增序号并更新快照。

    def state(self, job, stage, message, error_code=""):
        source = job["msg"]
        record = self.records.setdefault(source.request_id, {})
        previous = record.get("state")

        result = AudioProcessingState()
        result.stamp = self.node.get_clock().now().to_msg()
        result.source_node = self.node.get_fully_qualified_name()
        result.boot_id = self.boot_id
        result.event_seq = previous.event_seq + 1 if previous else 1
        for name in ("request_id", "session_id", "segment_id", "input_context_id"):
            setattr(result, name, getattr(source, name))
        result.audio_path = str(job["path"])

        phase = job.get("stage", "queued")
        result.operation = (
            "/ai/parse_task"
            if phase.startswith("parse")
            else (
                "/ai/recognize_audio"
                if phase.startswith("asr")
                else "/ai/process_audio"
            )
        )
        result.stage, result.message, result.error_code = stage, message, error_code

        record["state"] = result
        self.states.publish(result)

    # 识别与解析结果快照。

    def asr_result(self, result):
        if result.request_id in self.records:
            self.records[result.request_id]["asr"] = deepcopy(result)

    def parse_result(self, result):
        report = TaskParseReport()
        for name in report.get_fields_and_field_types():
            setattr(report, name, deepcopy(getattr(result, name)))
        self.records[result.request_id]["parse"] = report
        self.parses.publish(report)

    # 按请求 ID 查询已有结果。

    def query(self, request, response):
        response.stamp = self.node.get_clock().now().to_msg()
        response.request_id = request.request_id
        if not request.request_id.strip() or not request.target_request_id.strip():
            response.error_code, response.message = (
                "INVALID_REQUEST",
                "查询 ID 不能为空",
            )
            return response

        record = self.records.get(request.target_request_id)
        if record is None:
            response.error_code, response.message = (
                "AUDIO_NOT_FOUND",
                "当前进程没有该音频的处理记录",
            )
            return response

        response.success = response.found = True
        response.message = "已找到音频处理记录；查询成功不代表识别或语义解析成功"
        response.state = deepcopy(record["state"])
        if "asr" in record:
            response.asr_available, response.asr = True, deepcopy(record["asr"])
        if "parse" in record:
            response.parse_available, response.parse = True, deepcopy(record["parse"])
        return response
