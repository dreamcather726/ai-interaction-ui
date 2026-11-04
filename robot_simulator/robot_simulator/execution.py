"""Deterministic task scheduler and its authoritative simulated world; no ROS I/O."""

from copy import deepcopy
import math

from robot_simulator.plans import describe_step
from robot_simulator.scene import STATIONS


class ExecutionError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SimulationExecution:
    """All movement and material changes happen here, one primitive at a time."""

    def __init__(
        self,
        stations=None,
        objects=None,
        *,
        speed=0.8,
        action_seconds=2.0,
        step_timeout=60.0,
        fail_step=0,
    ):
        if (stations is not None and not isinstance(stations, dict)) or (
            objects is not None and not isinstance(objects, dict)
        ):
            raise ValueError("工位和物料配置必须为JSON对象")
        self.stations = dict(STATIONS if stations is None else stations)
        self.objects = dict({"box_1": "station_a"} if objects is None else objects)
        if not self.stations or any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(point, (list, tuple))
            or len(point) != 2
            or any(
                not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 4.5
                for v in point
            )
            for name, point in self.stations.items()
        ):
            raise ValueError(
                "stations_json 必须是工位ID到地图坐标[x,y]的映射，坐标在[-4.5,4.5]内"
            )
        if any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(station, str)
            or station not in self.stations
            for name, station in self.objects.items()
        ):
            raise ValueError("objects_json 必须是物料ID到已配置工位ID的映射")
        self.stations = {
            name: (float(point[0]), float(point[1]))
            for name, point in self.stations.items()
        }
        if any(
            not math.isfinite(value) or value <= 0
            for value in (speed, action_seconds, step_timeout)
        ):
            raise ValueError("速度、抓放耗时和步骤超时必须为有限正数")
        if fail_step < 0:
            raise ValueError("fail_step 必须为非负整数，0表示不注入故障")
        self.speed, self.action_seconds, self.step_timeout = (
            speed,
            action_seconds,
            step_timeout,
        )
        self.fail_step = fail_step
        self.x = self.y = self.yaw = self.velocity = 0.0
        self.held_object = ""
        self.state, self.error_code, self.message = "IDLE", "", "等待已确认任务"
        self.steps = []
        self.completed = 0
        self.step_elapsed = self.motion_elapsed = self.initial_distance = 0.0
        self.step_fraction = 0.0
        self.return_steps = set()

    def _station(self, station):
        if station not in self.stations:
            raise ExecutionError("UNKNOWN_STATION", f"模拟地图未配置工位 {station}")
        return self.stations[station]

    def sync_catalog(self, station_ids, object_ids, *, initial=False):
        """Assign SIM-only coordinates; preserve established material locations."""
        if self.state == "RUNNING":
            raise ExecutionError("TASK_BUSY", "执行期间不修改模拟场景，完成后同步资料")
        if not station_ids:
            raise ExecutionError("UNKNOWN_STATION", "请先在工位管理中添加并启用工位")
        if (
            len(station_ids) > 128
            or len(object_ids) > 128
            or len(set(station_ids)) != len(station_ids)
            or len(set(object_ids)) != len(object_ids)
        ):
            raise ExecutionError("INVALID_REQUEST", "模拟资料每类最多128条，ID不得重复")
        if initial and (self.steps or self.held_object):
            raise ExecutionError("SCENE_CONFLICT", "已有执行状态，不能重新初始化场景")
        previous = {} if initial else self.stations
        stations = {key: previous[key] for key in station_ids if key in previous}
        slots = [(-2.0, 0.0), (2.0, 0.0)] + [
            (x * 0.65, y * 0.65) for y in range(-6, 7) for x in range(-6, 7)
        ]
        occupied = set(stations.values())
        for key in station_ids:
            if key not in stations:
                point = next(point for point in slots if point not in occupied)
                stations[key] = point
                occupied.add(point)
        if self.held_object and self.held_object not in object_ids:
            raise ExecutionError(
                "SCENE_CONFLICT", "仍持有物料，不能从资料中移除；请先完成放置"
            )
        previous_objects = {} if initial else self.objects
        objects = {}
        for key in object_ids:
            location = previous_objects.get(key, station_ids[0])
            if location not in stations and key != self.held_object:
                raise ExecutionError(
                    "SCENE_CONFLICT",
                    f"物料 {key} 仍在已移除的工位 {location}，请先处置物料或重启模拟场景",
                )
            objects[key] = location
        self.stations, self.objects = stations, objects

    def _check_action(self, kind, station, object_id, x, y, held, objects):
        target = self._station(station)
        if kind not in ("NAVIGATE", "PICK", "PLACE"):
            raise ExecutionError("UNSUPPORTED_TASK", f"不支持动作 {kind}")
        if kind == "NAVIGATE":
            return
        if math.hypot(x - target[0], y - target[1]) > 0.03:
            raise ExecutionError(
                "NOT_AT_STATION", f"尚未到达 {station}，不能执行 {kind}"
            )
        if object_id not in objects:
            raise ExecutionError("UNKNOWN_OBJECT", f"模拟世界未配置物料 {object_id}")
        if kind == "PICK":
            if held:
                raise ExecutionError("GRIPPER_OCCUPIED", f"仍持有 {held}，不能再次抓取")
            if objects[object_id] != station:
                raise ExecutionError(
                    "OBJECT_NOT_AT_STATION", f"{object_id} 不在 {station}"
                )
        elif held != object_id:
            raise ExecutionError("NOT_HOLDING_OBJECT", f"未持有 {object_id}，不能放下")

    def validate_plan(self, steps):
        """Dry-run against current position/inventory; never change the world."""
        if not 1 <= len(steps) <= 256:
            raise ExecutionError("INVALID_REQUEST", "必须提供1～256个动作")
        x, y, held, objects = self.x, self.y, self.held_object, dict(self.objects)
        for kind, station, object_id in steps:
            self._check_action(kind, station, object_id, x, y, held, objects)
            if kind == "NAVIGATE":
                x, y = self.stations[station]
            elif kind == "PICK":
                held, objects[object_id] = object_id, ""
            else:
                held, objects[object_id] = "", station

    def start(self, steps, return_steps=()):
        if self.state == "RUNNING":
            raise ExecutionError("TASK_BUSY", "已有任务正在执行")
        self.validate_plan(steps)
        self.steps = list(steps)
        self.return_steps = set(return_steps)
        self.completed = 0
        self.step_fraction = 0.0
        self.error_code = ""
        self.state = "RUNNING"
        self._enter_step()

    def _enter_step(self):
        self.step_elapsed = self.motion_elapsed = self.step_fraction = self.velocity = (
            0.0
        )
        kind, station, object_id = self.steps[self.completed]
        self._check_action(
            kind, station, object_id, self.x, self.y, self.held_object, self.objects
        )
        target = self.stations[station]
        self.initial_distance = math.hypot(self.x - target[0], self.y - target[1])
        self.message = "正在执行：" + self.step_description

    @property
    def step_description(self):
        if not self.steps:
            return ""
        index = min(self.completed, len(self.steps) - 1)
        return describe_step(index + 1, len(self.steps), self.steps[index])

    @property
    def progress(self):
        if not self.steps:
            return 0.0
        if self.state == "SUCCEEDED":
            return 1.0
        return min(0.999, (self.completed + self.step_fraction) / len(self.steps))

    def tick(self, dt, *, wall_dt=None):
        if self.state != "RUNNING":
            return
        if (
            not math.isfinite(dt)
            or dt < 0
            or (wall_dt is not None and (not math.isfinite(wall_dt) or wall_dt < 0))
        ):
            raise ValueError("时间增量必须为非负有限数")
        kind, station, object_id = self.steps[self.completed]
        self.step_elapsed += dt if wall_dt is None else wall_dt
        if self.fail_step == self.completed + 1:
            self.fail("SIMULATED_FAILURE", f"第 {self.completed + 1} 步注入模拟故障")
            return
        if self.step_elapsed > self.step_timeout:
            self.fail("STEP_TIMEOUT", f"第 {self.completed + 1} 步等待完成超时")
            return
        try:
            self._check_action(
                kind, station, object_id, self.x, self.y, self.held_object, self.objects
            )
            if kind == "NAVIGATE":
                tx, ty = self.stations[station]
                dx, dy = tx - self.x, ty - self.y
                distance = math.hypot(dx, dy)
                travel = min(distance, self.speed * dt)
                if distance > 1e-9:
                    self.yaw = math.atan2(dy, dx)
                    self.x += dx / distance * travel
                    self.y += dy / distance * travel
                self.velocity = travel / dt if dt > 0 else 0.0
                remaining = math.hypot(tx - self.x, ty - self.y)
                self.step_fraction = (
                    1.0 - remaining / self.initial_distance
                    if self.initial_distance
                    else 1.0
                )
                done = remaining <= 1e-9
            else:
                self.velocity = 0.0
                self.motion_elapsed += dt
                self.step_fraction = min(1.0, self.motion_elapsed / self.action_seconds)
                done = self.motion_elapsed >= self.action_seconds
            if done:
                if kind == "PICK":
                    self.held_object, self.objects[object_id] = object_id, ""
                elif kind == "PLACE":
                    self.held_object, self.objects[object_id] = "", station
                self.completed += 1
                self.step_fraction = self.velocity = 0.0
                if self.completed == len(self.steps):
                    self.state, self.message = "SUCCEEDED", "模拟执行完成"
                else:
                    self._enter_step()
        except ExecutionError as exc:
            self.fail(exc.code, str(exc))

    def fail(self, code, message):
        if self.state != "RUNNING":
            return
        self.state, self.error_code, self.message = "FAILED", code, message
        self.velocity = 0.0

    def cancel(self):
        if self.state == "RUNNING":
            self.state, self.error_code, self.message = (
                "CANCELED",
                "CANCELED",
                "模拟执行已取消",
            )
            self.velocity = 0.0

    def frame(self):
        objects = []
        for object_id, station in sorted(self.objects.items()):
            held = object_id == self.held_object
            x, y = (self.x, self.y) if held else self.stations[station]
            objects.append(
                {
                    "id": object_id,
                    "station_id": station,
                    "carried": held,
                    "x": x,
                    "y": y,
                }
            )
        completed = self.steps[: self.completed]
        picks = sum(step[0] == "PICK" for step in self.steps)
        places = sum(step[0] == "PLACE" for step in self.steps)
        return {
            "x": self.x,
            "y": self.y,
            "yaw": self.yaw,
            "speed": self.velocity,
            "held_object_id": self.held_object,
            "objects": deepcopy(objects),
            "stations": deepcopy(self.stations),
            "state": self.state,
            "step": self.step_description,
            "step_index": min(self.completed + 1, len(self.steps)),
            "total_steps": len(self.steps),
            "completed_steps": self.completed,
            "progress": self.progress,
            "error_code": self.error_code,
            "message": self.message,
            "pickup_completed": bool(
                picks and sum(step[0] == "PICK" for step in completed) == picks
            ),
            "dropoff_completed": bool(
                places and sum(step[0] == "PLACE" for step in completed) == places
            ),
            "return_completed": bool(
                self.return_steps
                and all(index < self.completed for index in self.return_steps)
            ),
        }
