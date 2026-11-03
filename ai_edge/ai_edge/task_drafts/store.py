"""SQLite 草稿仓库：内容版本、幂等请求和人工确认绑定。"""

from ai_edge.common import error_codes as Codes

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ai_edge.semantic.semantic_contract import SemanticError
from ai_edge.task_drafts.actions import validate_action_plan


class DraftError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def plan_steps(candidate):
    if candidate["task_type"] == "SEQUENCE":
        return candidate["steps"]
    return [{key: value for key, value in candidate.items() if key != "steps"}]


def plan_summary(candidate):
    lines = []
    for index, step in enumerate(plan_steps(candidate), 1):
        if step["task_type"] == "NAVIGATE":
            text = f"导航到 {step['destination_station_id'] or '待补充'}"
        elif step["task_type"] in ("PICK", "PLACE"):
            picking = step["task_type"] == "PICK"
            station = (
                step["pickup_station_id"] if picking else step["dropoff_station_id"]
            )
            verb = "抓取" if picking else "放下"
            text = f"在 {station or '待补充工位'} {verb} {step['object_id'] or '待补充物料'}"
        else:
            text = (
                f"将 {step['object_id'] or '待补充物料'} 从 "
                f"{step['pickup_station_id'] or '待补充取货点'} 搬运到 "
                f"{step['dropoff_station_id'] or '待补充放货点'}"
            )
        if step["return_required"]:
            text += f"，随后返回 {step['return_station_id'] or '待补充返回点'}"
        lines.append(f"{index}. {text}")
    return "\n".join(lines)


class DraftStore:
    EDITING, CONFIRMED, CANCELED, EXPIRED = range(4)

    def __init__(self, path, stations, objects, timeout=300.0):
        if not 1 <= timeout <= 1800:
            raise ValueError("草稿确认窗口必须在 1～1800 秒内")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.stations, self.objects, self.timeout = (
            list(stations),
            list(objects),
            timeout,
        )
        self.boot_id = uuid4().hex
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, body TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS requests "
            "(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, boot TEXT NOT NULL, body TEXT NOT NULL);"
        )
        # 重启保留审计内容，但旧窗口和旧确认令牌全部失效。
        self.invalidate()

    def _write(self, draft):
        self.db.execute(
            "INSERT OR REPLACE INTO drafts VALUES (?, ?)",
            (draft["draft_id"], json.dumps(draft, ensure_ascii=False)),
        )

    @contextmanager
    def transaction(self):
        """事务入口唯一，提交失败和业务异常都回滚，禁止隐式事务混入。"""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def _get(self, draft_id):
        row = self.db.execute(
            "SELECT body FROM drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise DraftError(Codes.DRAFT_NOT_FOUND, "草稿不存在，请刷新列表")
        return json.loads(row[0])

    def invalidate(self):
        changed = []
        with self.transaction():
            for (body,) in self.db.execute("SELECT body FROM drafts").fetchall():
                draft = json.loads(body)
                if draft["state"] in (self.EDITING, self.CONFIRMED):
                    draft.update(
                        state=self.EXPIRED,
                        confirmation_token="",
                        event_seq=draft["event_seq"] + 1,
                    )
                    self._write(draft)
                    changed.append(draft)
        return changed

    def expire(self, now_ns):
        changed = []
        with self.transaction():
            for (body,) in self.db.execute("SELECT body FROM drafts").fetchall():
                draft = json.loads(body)
                if draft["state"] == self.EDITING and now_ns >= draft["expires_ns"]:
                    draft.update(
                        state=self.EXPIRED,
                        confirmation_token="",
                        event_seq=draft["event_seq"] + 1,
                    )
                    self._write(draft)
                    changed.append(draft)
        return changed

    def query(self, draft_id=""):
        if draft_id:
            return [self._get(draft_id)]
        rows = self.db.execute(
            "SELECT body FROM drafts ORDER BY rowid DESC LIMIT 100"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def apply(self, request_id, operation, value, now_ns):
        if not request_id.strip() or len(request_id) > 256 or now_ns <= 0:
            raise DraftError(Codes.INVALID_REQUEST, "请求 ID 或 ROS 时间不合法")
        fingerprint = hashlib.sha256(
            json.dumps([operation, value], ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()
        with self.transaction():
            cached = self.db.execute(
                "SELECT fingerprint,boot,body FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if cached:
                if cached[0] != fingerprint:
                    raise DraftError(
                        Codes.REQUEST_ID_CONFLICT, "相同请求 ID 对应了不同操作"
                    )
                if cached[1] != self.boot_id:
                    raise DraftError(
                        Codes.DRAFT_RESTARTED, "草稿服务已重启，请刷新后重新审阅"
                    )
                # 返回当前状态，避免幂等重放把界面回退到旧状态。
                return self._get(json.loads(cached[2])["draft_id"])
            if operation == "save":
                draft = self._save(value, now_ns)
            else:
                draft = self._review(operation, value, now_ns)
            self._write(draft)
            self.db.execute(
                "INSERT INTO requests VALUES (?,?,?,?)",
                (
                    request_id,
                    fingerprint,
                    self.boot_id,
                    json.dumps(draft, ensure_ascii=False),
                ),
            )
        return draft

    def _save(self, value, now_ns):
        for field in ("session_id", "input_context_id"):
            if not value[field].strip() or len(value[field]) > 256:
                raise DraftError(
                    Codes.INVALID_REQUEST, "草稿来源会话和上下文不能为空或超长"
                )
        sources = value["source_request_ids"]
        if len(sources) > 512 or any(
            not item.strip() or len(item) > 256 for item in sources
        ):
            raise DraftError(Codes.INVALID_REQUEST, "来源请求列表无效")
        try:
            prepared = validate_action_plan(
                value["candidate"], self.stations, self.objects
            )
        except SemanticError as exc:
            raise DraftError(exc.code, str(exc)) from exc
        if value["draft_id"]:
            old = self._get(value["draft_id"])
            if value["expected_revision"] != old["revision"]:
                raise DraftError(
                    Codes.DRAFT_VERSION_CHANGED, "草稿已被修改，请刷新后再操作"
                )
            if (value["session_id"], value["input_context_id"]) != (
                old["session_id"],
                old["input_context_id"],
            ):
                raise DraftError(Codes.DRAFT_CONTEXT_MISMATCH, "不能修改草稿所属的会话")
        else:
            if value["expected_revision"] != 0:
                raise DraftError(Codes.DRAFT_VERSION_CHANGED, "新草稿版本必须为零")
            old = dict(draft_id="draft-" + uuid4().hex, revision=0, event_seq=0)
        return dict(
            draft_id=old["draft_id"],
            revision=old["revision"] + 1,
            event_seq=old["event_seq"] + 1,
            boot_id=self.boot_id,
            state=self.EDITING,
            session_id=value["session_id"],
            input_context_id=value["input_context_id"],
            source_request_ids=list(dict.fromkeys(sources)),
            candidate=prepared,
            summary=plan_summary(prepared),
            confirmation_token=uuid4().hex,
            expires_ns=now_ns + int(self.timeout * 10**9),
            updated_ns=now_ns,
        )

    def _review(self, operation, value, now_ns):
        if operation not in ("confirm", "cancel"):
            raise DraftError(Codes.INVALID_REQUEST, "不支持的草稿操作")
        draft = self._get(value["draft_id"])
        for field in ("boot_id", "session_id", "input_context_id"):
            if value[field] != draft[field] or draft["boot_id"] != self.boot_id:
                raise DraftError(
                    Codes.DRAFT_CONTEXT_MISMATCH, "草稿来源或服务实例已变化，请刷新"
                )
        if value["revision"] != draft["revision"]:
            raise DraftError(Codes.DRAFT_VERSION_CHANGED, "草稿版本已变化，请重新审阅")
        if (
            value["confirmation_token"] != draft["confirmation_token"]
            or not value["confirmation_token"]
        ):
            raise DraftError(
                Codes.DRAFT_TOKEN_INVALID, "确认信息已失效，请重新保存草稿"
            )
        if operation == "confirm":
            if draft["state"] != self.EDITING or now_ns >= draft["expires_ns"]:
                raise DraftError(
                    Codes.DRAFT_EXPIRED, "草稿不在可确认状态，请重新保存后审阅"
                )
            try:
                prepared = validate_action_plan(
                    draft["candidate"], self.stations, self.objects
                )
            except SemanticError as exc:
                raise DraftError(exc.code, str(exc)) from exc
            if prepared != draft["candidate"]:
                raise DraftError(
                    Codes.DRAFT_REVIEW_REQUIRED, "旧草稿须先展开并重新保存，再逐步确认"
                )
            draft["state"] = self.CONFIRMED
        else:
            if draft["state"] not in (self.EDITING, self.CONFIRMED):
                raise DraftError(Codes.DRAFT_CLOSED, "草稿已经关闭")
            draft["state"] = self.CANCELED
            draft["confirmation_token"] = ""
        draft.update(updated_ns=now_ns, event_seq=draft["event_seq"] + 1)
        return draft

    def close(self):
        self.db.close()
