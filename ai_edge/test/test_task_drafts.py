"""草稿和长步骤列表的离线回归用例；需要用户手动运行。"""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from ai_edge.semantic.semantic_contract import (
    SemanticError,
    empty_candidate,
    validate_candidate,
)
from ai_edge.task_drafts.store import DraftError, DraftStore, plan_steps


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "drafts.sqlite3"
        self.store = DraftStore(
            self.path, ["station_a", "station_b"], ["box_1"], timeout=10
        )
        self.step = dict(
            task_type="TRANSPORT",
            object_id="box_1",
            pickup_station_id="station_a",
            dropoff_station_id="station_b",
            destination_station_id="",
            return_required=False,
            return_station_id="",
        )
        self.value = dict(
            draft_id="",
            expected_revision=0,
            session_id="session",
            input_context_id="context",
            source_request_ids=["audio-1"],
            candidate=empty_candidate(
                "SEQUENCE", [deepcopy(self.step) for _ in range(40)]
            ),
        )

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def save(self, request_id="save-1", value=None):
        return self.store.apply(request_id, "save", value or self.value, 10**9)

    def review(self, draft, operation="confirm", request_id="confirm-1", now=2 * 10**9):
        return self.store.apply(request_id, operation, draft, now)

    def test_variable_length_draft_and_confirmation_are_persisted(self):
        draft = self.save()
        self.assertEqual(len(plan_steps(draft["candidate"])), 160)
        self.assertEqual(draft["revision"], 1)
        result = self.review(draft)
        self.assertEqual(result["state"], DraftStore.CONFIRMED)
        self.assertEqual(self.store.query(draft["draft_id"])[0], result)

    def test_failed_save_rolls_back_and_following_request_can_commit(self):
        invalid = deepcopy(self.value)
        invalid["candidate"]["steps"][0]["object_id"] = "unknown_box"
        with self.assertRaises(DraftError):
            self.save("bad-save", invalid)
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.query(), [])
        saved = self.save("good-save")
        self.assertEqual(saved["revision"], 1)
        self.assertFalse(self.store.db.in_transaction)

    def test_same_save_is_idempotent_and_conflicting_request_is_rejected(self):
        first = self.save()
        self.assertEqual(self.save()["draft_id"], first["draft_id"])
        value = deepcopy(self.value)
        value["source_request_ids"] = ["audio-2"]
        with self.assertRaises(DraftError) as caught:
            self.save(value=value)
        self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")

    def test_edit_invalidates_old_review(self):
        first = self.save()
        self.review(first)
        value = deepcopy(self.value)
        value.update(draft_id=first["draft_id"], expected_revision=1)
        value["candidate"]["steps"].pop()
        second = self.save("save-2", value)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["state"], DraftStore.EDITING)
        self.assertNotEqual(first["confirmation_token"], second["confirmation_token"])
        with self.assertRaises(DraftError):
            self.review(first, request_id="old-confirm")

    def test_expired_token_and_wrong_context_cannot_confirm(self):
        draft = self.save()
        wrong = dict(draft, input_context_id="other")
        with self.assertRaises(DraftError):
            self.review(wrong)
        with self.assertRaises(DraftError):
            self.review(draft, now=12 * 10**9)
        self.store.expire(12 * 10**9)
        self.assertEqual(self.store.query()[0]["state"], DraftStore.EXPIRED)

    def test_restart_keeps_content_and_invalidates_confirmation(self):
        draft = self.save()
        self.review(draft)
        self.store.close()
        self.store = DraftStore(self.path, ["station_a", "station_b"], ["box_1"])
        restored = self.store.query(draft["draft_id"])[0]
        self.assertEqual(restored["state"], DraftStore.EXPIRED)
        self.assertEqual(len(restored["candidate"]["steps"]), 160)
        with self.assertRaises(DraftError):
            self.review(draft, request_id="after-restart")

    def test_missing_step_field_prevents_draft_confirmation(self):
        value = deepcopy(self.value)
        value["candidate"]["steps"][20]["object_id"] = ""
        answer = validate_candidate(
            value["candidate"], ["station_a", "station_b"], ["box_1"]
        )
        self.assertIn("steps[20].object_id", answer.missing_fields)
        self.assertIn("第 21 步", answer.clarification_question)
        with self.assertRaises(DraftError):
            self.save(value=value)

    def test_cancel_does_not_report_robot_execution(self):
        draft = self.save()
        canceled = self.review(draft, "cancel", "cancel-1")
        self.assertEqual(canceled["state"], DraftStore.CANCELED)
        self.assertEqual(canceled["confirmation_token"], "")

    def test_conflicting_revisions_cannot_overwrite_steps(self):
        draft = self.save()
        update = dict(self.value, draft_id=draft["draft_id"], expected_revision=0)
        with self.assertRaises(DraftError) as caught:
            self.save("stale-save", update)
        self.assertEqual(caught.exception.code, "DRAFT_VERSION_CHANGED")

    def test_over_limit_sequence_is_rejected_without_truncation(self):
        data = empty_candidate("SEQUENCE", [self.step] * 257)
        with self.assertRaises(SemanticError):
            validate_candidate(data, ["station_a", "station_b"], ["box_1"])


if __name__ == "__main__":
    unittest.main()
