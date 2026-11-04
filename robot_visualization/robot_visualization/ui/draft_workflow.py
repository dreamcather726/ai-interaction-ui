"""Homepage shortcuts to the existing draft editor and its asynchronous receipts."""

from copy import deepcopy
import time

from PySide6.QtCore import QObject, Slot

from ai_edge.task_drafts.actions import validate_action_plan
from ai_edge.task_drafts.store import plan_summary
from robot_visualization.task_model import normalize_candidate


class HomeDraftWorkflow(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.panel = window.draft_panel
        self.error = ""
        self.operation = ""
        self.preflight_requested = None
        self.authorization = None
        self.submitted_key = None
        self.preview_text = None
        window.home_add_candidate_button.clicked.connect(self.add_candidate)
        window.home_save_draft_button.clicked.connect(self.save)
        window.home_confirm_draft_button.clicked.connect(self.panel.confirm)
        self.panel.command_requested.connect(self.on_command)
        if window.robot_view is not None:
            self.panel.execution_ready = False
            self.panel.confirmation_gate = self.confirm_execution
            self.panel.buttons["confirm"].setText("确认并整单执行")
            window.retry_simulation_button.clicked.connect(self.retry_preparation)
        self.refresh()

    def task(self):
        view = self.window.robot_view
        return view.task if view is not None else {}

    @Slot(str, dict)
    def on_command(self, operation, value):
        self.error = ""
        self.operation = operation
        self.refresh()

    def receive(self, value):
        if value.get("operation") != "event":
            self.error = (
                "" if value.get("success") else value.get("message", "草稿请求失败")
            )
            self.operation = ""
        self.refresh()
        if value.get("operation") == "confirm":
            draft = value.get("draft")
            authorized = self.authorization
            self.authorization = None
            if (
                value.get("success")
                and draft
                and authorized == self.draft_key(draft)
                and self.prepared_ready()
            ):
                self.submitted_key = self.draft_key(draft)
                self.window.draft_requested.emit(
                    "execute", self.execution_payload(draft)
                )
                self.refresh()

    def draft_key(self, draft):
        return (
            draft["draft_id"],
            draft["revision"],
            draft["boot_id"],
            self.window.catalog_snapshot["revision"],
        )

    def execution_payload(self, draft):
        return dict(
            deepcopy(draft), catalog_revision=self.window.catalog_snapshot["revision"]
        )

    def prepared_ready(self):
        saved, task = self.panel.saved, self.task()
        return bool(
            saved
            and saved["state"] in (0, 1)
            and not self.panel.dirty
            and not self.panel.unknown
            and task.get("prepared")
            and self.submitted_key != self.draft_key(saved)
            and (
                task.get("draft_id"),
                task.get("draft_revision"),
                task.get("draft_boot_id"),
                task.get("catalog_revision"),
            )
            == (
                saved["draft_id"],
                saved["revision"],
                saved["boot_id"],
                self.window.catalog_snapshot["revision"],
            )
            and time.monotonic() < task.get("prepared_until", 0)
        )

    @Slot()
    def confirm_execution(self):
        self.refresh()
        if (
            not self.window.home_confirm_draft_button.isEnabled()
            or not self.prepared_ready()
        ):
            return
        if (
            self.window.pages.currentIndex() == self.window.page_indexes["首页"]
            and self.window.command_pages.currentIndex() != 1
        ):
            self.window.command_pages.setCurrentIndex(1)
            self.error = "请先核对完整草稿，再点击确认并整单执行"
            self.refresh()
            return
        self.window.command_pages.setCurrentIndex(1)
        self.authorization = self.draft_key(self.panel.saved)
        if self.panel.saved["state"] == 0:
            self.panel.send("confirm", deepcopy(self.panel.saved))
        elif self.panel.saved["state"] == 1:
            self.authorization = None
            self.submitted_key = self.draft_key(self.panel.saved)
            self.window.draft_requested.emit(
                "execute", self.execution_payload(self.panel.saved)
            )
            self.refresh()

    def refresh_preview(self):
        panel, window = self.panel, self.window
        try:
            plan = panel.plan()
            suggested = self.task().get("revised_candidate")
            if (
                suggested
                and not panel.dirty
                and panel.saved
                and self.task().get("draft_id") == panel.saved["draft_id"]
            ):
                plan = normalize_candidate(suggested)
            if self.prepared_ready():
                plan = normalize_candidate(self.task()["prepared_candidate"])
            rendered = deepcopy(plan)
            names = {
                kind: {
                    row["id"]: (
                        f"{row['name']} [{row['id']}]"
                        if row["name"] != row["id"]
                        else row["id"]
                    )
                    for row in window.catalog_snapshot[kind]
                }
                for kind in ("stations", "objects")
            }
            for step in rendered.get("steps", []) or [rendered]:
                for field in (
                    "object_id",
                    "pickup_station_id",
                    "dropoff_station_id",
                    "destination_station_id",
                    "return_station_id",
                ):
                    mapping = names["objects" if field == "object_id" else "stations"]
                    step[field] = mapping.get(step.get(field), step.get(field, ""))
            text = plan_summary(rendered)
            if suggested:
                text = (
                    "执行端建议调整（未保存、未授权执行）：\n请进入编辑面板核对并保存新版本。\n\n"
                    + text
                )
        except ValueError:
            text = "尚无任务草稿。请加入语音候选，或点击下方按钮手动编排。"
        if text != self.preview_text:
            self.preview_text = text
            window.home_draft_preview.setPlainText(text)
        if panel.saved:
            suffix = (
                "有未保存修改"
                if panel.dirty
                else (
                    "预检通过，等待你确认"
                    if self.prepared_ready()
                    else "已保存，等待预检／核对"
                )
            )
            window.home_draft_identity.setText(
                f"草稿 {panel.saved['draft_id']} · v{panel.saved['revision']} · {suffix}"
            )
        else:
            window.home_draft_identity.setText("任务草稿 · 未保存")

    @Slot()
    def add_candidate(self):
        self.refresh()
        if not self.window.home_add_candidate_button.isEnabled():
            return
        parsed = deepcopy(self.panel.candidate)
        # Validate before new(), so a stale catalog cannot erase the existing draft.
        try:
            validate_action_plan(
                normalize_candidate(parsed["candidate"]),
                self.panel.stations,
                self.panel.objects,
            )
        except (ValueError, KeyError, TypeError) as exc:
            self.error = f"候选暂不能加入草稿：{exc}"
            self.refresh()
            return
        self.error = ""
        self.panel.new()
        self.panel.set_candidate(parsed)
        self.panel.add_candidate()
        self.window.command_pages.setCurrentIndex(1)
        self.panel.refresh_controls()
        if parsed["request_id"] not in self.panel.sources:
            self.error = self.panel.info.text()
            self.refresh()
            return
        self.save()

    @Slot()
    def retry_preparation(self):
        self.refresh()
        if self.window.retry_simulation_button.isEnabled():
            self.submitted_key = None
            self.window.retry_simulation_button.setEnabled(False)
            self.window.draft_requested.emit(
                "prepare_execution", self.execution_payload(self.panel.saved)
            )

    @Slot()
    def save(self):
        if self.panel.buttons["save"].isEnabled() and not self.window.closing:
            self.panel.save()
            if not self.panel.pending:
                self.error = self.panel.info.text()
        elif not self.panel.online:
            self.error = "草稿服务未连接，步骤已保留；连接后可点击保存草稿"
        self.refresh()

    def refresh(self):
        window, panel, task = self.window, self.panel, self.task()
        active = task.get("active", False)
        unknown = task.get("execution_unknown", False)
        free = not window.closing and not panel.pending and not active and not unknown
        if window.robot_view is not None:
            panel.execution_ready = self.prepared_ready()
            panel.refresh_controls()
        candidate = panel.candidate
        duplicate = bool(candidate and candidate.get("request_id") in panel.sources)
        unsaved_edits = panel.dirty and panel.table.rowCount() > 0
        can_add = bool(
            candidate
            and free
            and not duplicate
            and not unsaved_edits
            and not panel.unknown
        )
        window.home_add_candidate_button.setEnabled(can_add)
        if duplicate:
            reason = "当前候选已加入草稿，不重复添加"
        elif unsaved_edits:
            reason = "已有未保存的步骤，请先保存；不会覆盖未保存草稿"
        elif unknown:
            reason = "原任务结果未知，需先核实原执行；不能再次下发"
        elif active:
            reason = "请等待当前任务结束"
        else:
            reason = "新建草稿、填入当前候选并自动保存；确认仍需手动点击"
        window.home_add_candidate_button.setToolTip(reason)
        window.home_confirm_draft_button.setEnabled(
            free and panel.buttons["confirm"].isEnabled()
        )
        window.home_save_draft_button.setVisible(
            bool(panel.table.rowCount() and (panel.dirty or panel.saved is None))
        )
        window.home_save_draft_button.setEnabled(
            free and panel.buttons["save"].isEnabled()
        )

        saved = panel.saved
        same_task = bool(
            saved
            and task.get("draft_id") == saved["draft_id"]
            and task.get("draft_revision") == saved["revision"]
        )
        if window.robot_view is not None:
            retryable = bool(task.get("retryable") and same_task)
            window.retry_simulation_button.setVisible(retryable)
            window.retry_simulation_button.setEnabled(
                retryable
                and free
                and not panel.dirty
                and not panel.unknown
                and saved["state"] in (0, 1)
            )
        if unknown:
            status = "执行结果未知：" + task.get("message", "请核实原任务，不重复下发")
        elif active and task.get("preparation_pending"):
            status = "正在预检草稿，尚未下发执行"
        elif active and task.get("canceling"):
            status = "取消处理中，等待停止确认"
        elif active:
            step = task.get("step", "").partition(" ")[0]
            status = "执行中，步骤 " + step if step else "正在准备执行"
        elif panel.pending:
            status = {
                "save": "草稿未保存，正在保存……",
                "confirm": "正在确认草稿……",
            }.get(self.operation, "草稿请求处理中，请稍候")
        elif panel.unknown:
            status = "草稿结果未确认，请刷新已保存版本"
        elif panel.dirty or saved is None:
            status = "草稿未保存"
        elif same_task and task.get("state") == 3 and not task.get("failed"):
            status = "执行完成"
        elif same_task and (
            task.get("state") == 5 or task.get("error_code") == "CANCELED"
        ):
            status = "执行已取消"
        elif same_task and task.get("failed"):
            status = (
                "预检未通过，尚未执行：" if task.get("retryable") else "执行失败："
            ) + task.get("message", "请查看日志")
        elif self.prepared_ready():
            status = "预检通过，请在左侧核对完整草稿后确认整单执行"
        elif saved["state"] == 0:
            status = (
                "草稿已保存，等待预检和核对"
                if window.robot_view is not None
                else "草稿已保存，等待确认"
            )
        elif saved["state"] == 1:
            status = (
                "草稿已确认，等待执行"
                if window.robot_view is not None
                else "草稿已确认"
            )
        elif saved["state"] == 2:
            status = "草稿已取消"
        else:
            status = "草稿已失效，请重新保存"
        if self.error:
            status += " · " + self.error
        window.home_draft_status.setText(status)
        self.refresh_preview()
        if (
            window.robot_view is not None
            and free
            and saved
            and not panel.dirty
            and not panel.unknown
            and saved["state"] in (0, 1)
        ):
            key = self.draft_key(saved)
            if key != self.preflight_requested:
                self.preflight_requested = key
                window.draft_requested.emit(
                    "prepare_execution", self.execution_payload(saved)
                )
