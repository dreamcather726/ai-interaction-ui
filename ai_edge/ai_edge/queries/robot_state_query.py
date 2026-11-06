"""只读遥测快照与答复格式化；不依赖 ROS，不猜测机器人所在工位。"""

import math
import time

from ai_edge.semantic.interaction_contract import QUERY_TYPES


class RobotStateCache:
    def __init__(self, max_age=2.0, clock=time.monotonic):
        self.max_age, self.clock = max_age, clock
        self.samples = {}

    def update(self, query_type, data, stamp_ns, source):
        self.samples[query_type] = dict(
            data=data, stamp_ns=stamp_ns, received_at=self.clock(), source=source
        )

    def query(self, query_type, now_ns):
        if query_type not in QUERY_TYPES:
            return False, "INVALID_REQUEST", "当前不支持这项查询。", {}
        sample = self.samples.get(query_type)
        if sample is None:
            return (
                False,
                "ROBOT_DATA_UNAVAILABLE",
                "还没有收到机器人的这项实时数据，暂时无法查询。",
                {},
            )
        age = (now_ns - sample["stamp_ns"]) / 1e9
        if (
            self.clock() - sample["received_at"] > self.max_age
            or sample["stamp_ns"] <= 0
            or not -0.5 <= age <= self.max_age
        ):
            return (
                False,
                "ROBOT_DATA_STALE",
                "收到的机器人数据已过期或时间不同步，暂时无法确认当前情况。",
                {},
            )
        data = dict(sample["data"])
        evidence = dict(data=data, stamp_ns=sample["stamp_ns"], source=sample["source"])
        if query_type == "ROBOT_LOCATION":
            values = [data.get(key) for key in ("x", "y", "z", "yaw")]
            if not data.get("frame_id") or not all(
                isinstance(v, (float, int)) and math.isfinite(v) for v in values
            ):
                return (
                    False,
                    "INVALID_ROBOT_DATA",
                    "定位数据无效，暂时无法确认当前位置。",
                    {},
                )
            reply = (
                f"当前位置：x={data['x']:.2f}米，y={data['y']:.2f}米，"
                f"z={data['z']:.2f}米，朝向={math.degrees(data['yaw']):.1f}度。"
                f"坐标系：{data['frame_id']}；数据来源：{sample['source']}。"
            )
        elif query_type == "ROBOT_BATTERY":
            percentage = data.get("percentage")
            if (
                not isinstance(percentage, (float, int))
                or not math.isfinite(percentage)
                or not 0 <= percentage <= 1
            ):
                return (
                    False,
                    "INVALID_ROBOT_DATA",
                    "电池尚未提供有效电量，暂时无法查询剩余百分比。",
                    {},
                )
            reply = (
                f"当前电量约为{percentage * 100:.0f}%。数据来源：{sample['source']}。"
            )
        else:
            names = {
                1: "空闲",
                2: "正在执行任务",
                3: "正在停止",
                4: "急停",
                5: "故障",
                6: "正在恢复",
            }
            state = names.get(data.get("state"))
            if state is None:
                return False, "INVALID_ROBOT_DATA", "机器人尚未报告明确的运行状态。", {}
            reply = f"机器人当前状态：{state}。"
            if data.get("active_task_id"):
                reply += f"当前任务：{data['active_task_id']}。"
            if data.get("error_code"):
                reply += f"错误码：{data['error_code']}。"
            reply += f"数据来源：{sample['source']}。"
        reply += f"采样时间：{sample['stamp_ns'] / 1e9:.3f}秒（ROS时钟）。"
        return True, "", reply, evidence
