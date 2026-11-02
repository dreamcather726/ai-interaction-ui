"""界面记录缓存与乱序过滤；不触发模型或机器人任务。"""

from collections import OrderedDict

STAGES = {
    0: ("已受理", "active"),
    1: ("等待语音识别", "active"),
    2: ("正在识别语音", "active"),
    3: ("等待语义解析", "active"),
    4: ("正在理解任务", "active"),
    5: ("候选已生成", "good"),
    6: ("需要补充信息", "warn"),
    7: ("处理失败", "bad"),
    8: ("已取消", "idle"),
    9: ("未检测到语音", "warn"),
    10: ("语音识别完成", "good"),
}


class RecordStore:
    """按来源请求关联结果，保留最近 100 条；旧进程消息不覆盖新状态。"""

    def __init__(self, limit=100):
        self.limit = limit
        self.records = OrderedDict()
        self.boots = {}
        self.retired_boots = set()

    def apply(self, kind, value):
        request_id = value.get("request_id", "")
        if not request_id:
            return False
        record = self.records.get(request_id, {})
        previous = record.get(kind)
        if kind == "state":
            source, boot = value["source_node"], value["boot_id"]
            if (source, boot) in self.retired_boots:
                return False
            current = self.boots.get(source)
            if current and current[0] != boot:
                if value["stamp_ns"] < current[1]:
                    return False
                self.retired_boots.add((source, current[0]))
            self.boots[source] = (
                boot,
                max(value["stamp_ns"], current[1] if current else 0),
            )
            if previous and previous["boot_id"] != boot:
                record = {}
            if (
                previous
                and previous["boot_id"] == boot
                and value["event_seq"] <= previous["event_seq"]
            ):
                return False
        elif previous and value.get("stamp_ns", 0) < previous.get("stamp_ns", 0):
            return False
        record[kind] = value
        self.records[request_id] = record
        while len(self.records) > self.limit:
            self.records.popitem(last=False)
        return True
