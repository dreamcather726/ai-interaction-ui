"""澄清边界的离线回归用例；不启动 ROS、Qt 或模型。"""

import json
import unittest

from ai_edge.semantic.dialogue import DialogueStore, dialogue_command
from ai_edge.semantic.ollama_backend import OllamaBackend
from ai_edge.semantic.semantic_contract import (
    SemanticError,
    reconcile_candidate,
    validate_candidate,
)


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.store = DialogueStore(timeout=300.0, clock=lambda: self.now)
        self.key = ("session-1", "context-1")
        self.stations, self.objects = ["station_a", "station_b"], ["box_1"]
        self.partial = dict(
            task_type="TRANSPORT",
            object_id="",
            pickup_station_id="station_a",
            dropoff_station_id="station_b",
            destination_station_id="",
            return_required=False,
            return_station_id="",
            steps=[],
        )
        self.answer = validate_candidate(self.partial, self.stations, self.objects)
        self.store.remember(
            self.key, "去 A 点拿东西，然后到 B 点放下", self.answer, 100
        )

    def get(self, key=None, captured=101, stations=None):
        return self.store.get(
            key or self.key,
            captured,
            self.stations if stations is None else stations,
            self.objects,
        )

    def test_followup_completes_transport_without_losing_route(self):
        pending = self.get()
        self.assertEqual(pending.missing_fields, ("object_id",))
        self.assertEqual(pending.context[-1]["text"], "要搬运哪个物料？")
        # 模型续轮遗漏已知地点时，规则层补回原候选的地点。
        output = dict(
            self.partial, object_id="box_1", pickup_station_id="", dropoff_station_id=""
        )
        merged = reconcile_candidate(output, pending.candidate)
        checked = validate_candidate(merged, self.stations, self.objects)
        self.assertFalse(checked.missing_fields)
        self.assertEqual(checked.candidate["pickup_station_id"], "station_a")
        self.assertEqual(checked.candidate["dropoff_station_id"], "station_b")

    def test_cannot_borrow_candidate_from_other_session_or_context(self):
        self.assertIsNone(self.get(("session-2", "context-1")))
        self.assertIsNone(self.get(("session-1", "context-2")))

    def test_capture_before_question_is_not_an_answer(self):
        with self.assertRaises(SemanticError) as caught:
            self.get(captured=99)
        self.assertEqual(caught.exception.code, "CLARIFICATION_NOT_READY")
        self.assertIsNotNone(self.get())

    def test_expired_dialogue_requires_full_task_again(self):
        self.now = 300.0
        with self.assertRaises(SemanticError) as caught:
            self.get()
        self.assertEqual(caught.exception.code, "DIALOGUE_EXPIRED")
        self.assertIsNone(self.get())

    def test_changed_whitelist_invalidates_old_candidate(self):
        with self.assertRaises(SemanticError) as caught:
            self.get(stations=["station_b"])
        self.assertEqual(caught.exception.code, "DIALOGUE_CONFIG_CHANGED")
        self.assertIsNone(self.get())

    def test_conflicting_route_does_not_replace_pending_candidate(self):
        with self.assertRaises(SemanticError) as caught:
            reconcile_candidate(
                dict(self.partial, pickup_station_id="station_b"), self.get().candidate
            )
        self.assertEqual(caught.exception.code, "DIALOGUE_CONFLICT")
        self.assertEqual(self.get().candidate["pickup_station_id"], "station_a")

    def test_unknown_reply_does_not_complete_old_task(self):
        unknown = dict(
            self.partial,
            task_type="UNKNOWN",
            pickup_station_id="",
            dropoff_station_id="",
        )
        answer = validate_candidate(
            reconcile_candidate(unknown, self.partial), self.stations, self.objects
        )
        self.assertTrue(answer.unsupported)

    def test_backend_sends_context_and_merges_followup_response(self):
        backend = OllamaBackend()
        sent = []
        output = dict(
            self.partial, object_id="box_1", pickup_station_id="", dropoff_station_id=""
        )

        def fake_post(endpoint, body):
            sent.append(json.loads(body["messages"][-1]["content"]))
            return {"done": True, "message": {"content": json.dumps(output)}}

        backend.post = fake_post
        pending = self.get()
        answer = backend.parse(
            "一号箱子", self.stations, self.objects, pending.context, pending.candidate
        )
        self.assertEqual(sent[0]["partial_candidate"], self.partial)
        self.assertEqual(sent[0]["context"][-1]["text"], "要搬运哪个物料？")
        self.assertFalse(answer.missing_fields)
        self.assertEqual(answer.candidate["object_id"], "box_1")
        self.assertEqual(answer.candidate["pickup_station_id"], "station_a")
        self.assertEqual(answer.candidate["dropoff_station_id"], "station_b")

    def test_snapshot_mutation_does_not_modify_stored_task(self):
        pending = self.get()
        pending.candidate["pickup_station_id"] = "station_b"
        pending.context.clear()
        self.assertEqual(self.get().candidate["pickup_station_id"], "station_a")
        self.assertTrue(self.get().context)

    def test_cancel_is_scoped_to_one_context(self):
        other = ("session-2", "context-1")
        self.store.remember(other, "搬运", self.answer, 100)
        self.store.forget(self.key)
        self.assertIsNone(self.get())
        self.assertIsNotNone(self.get(other))
        self.assertEqual(dialogue_command("取消任务。"), "cancel")
        self.assertEqual(dialogue_command("重新开始！"), "reset")
        self.assertEqual(dialogue_command("搬完取消返回"), "")

    def test_capacity_cannot_silently_drop_another_task(self):
        store = DialogueStore(limit=1)
        store.remember(self.key, "搬运", self.answer, 100)
        with self.assertRaises(SemanticError) as caught:
            store.remember(("other", "context"), "搬运", self.answer, 100)
        self.assertEqual(caught.exception.code, "DIALOGUE_BUSY")
        self.assertIn(self.key, store.pending)


if __name__ == "__main__":
    unittest.main()
