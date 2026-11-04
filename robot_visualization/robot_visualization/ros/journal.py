"""Execution journal persistence; task state remains on the owning client."""

import json
from pathlib import Path

from robot_interfaces.action import ExecuteTask
from robot_interfaces.msg import TaskSnapshot
from robot_interfaces.srv import PrepareTask
from rosidl_runtime_py.convert import message_to_ordereddict
from rosidl_runtime_py.set_message import set_message_fields


class JournalStore:
    def __init__(self, client):
        self.client = client

    def load(self):
        client = self.client
        self.path = Path(client.config.journal_path).expanduser().resolve()
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            client.dispatched = {tuple(key) for key in value["dispatched"]}
            client.goal = ExecuteTask.Goal()
            set_message_fields(client.goal, value["goal"])
            client.prepare_request = PrepareTask.Request()
            set_message_fields(client.prepare_request.task, value["task"])
            client.source_draft = value["source_draft"]
            client.attempt_key = tuple(value["attempt_key"])
            client.execution_boot = value.get("boot_id")
            client.uncertain = True
            client.restored = True
            if value.get("terminal") is not None:
                snapshot = TaskSnapshot()
                set_message_fields(snapshot, value["terminal"])
                if snapshot.state not in (3, 4, 5) or not client.snapshot_matches(
                    snapshot
                ):
                    raise ValueError("本地终态与原下发记录不匹配")
                client.terminal_snapshot, client.replay_snapshot = snapshot, snapshot
                client.uncertain, client.restored = False, False
        except (ValueError, KeyError, TypeError, OSError, AttributeError) as exc:
            client.journal_error = (
                f"执行记录无法恢复，禁止新下发：{exc}；请先人工核对执行端"
            )

    def save(self):
        client = self.client
        value = dict(
            dispatched=sorted(client.dispatched),
            goal=dict(message_to_ordereddict(client.goal)),
            task=dict(message_to_ordereddict(client.prepare_request.task)),
            source_draft=client.source_draft,
            attempt_key=client.attempt_key,
            boot_id=client.execution_boot,
            terminal=(
                dict(message_to_ordereddict(client.terminal_snapshot))
                if client.terminal_snapshot is not None
                else None
            ),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            import os

            os.fsync(stream.fileno())
        temporary.replace(self.path)
