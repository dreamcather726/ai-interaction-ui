"""Draft-node startup and service callback regressions; run manually in Humble."""

from pathlib import Path
import tempfile
import unittest

try:
    import rclpy
    from ai_edge.common.catalog import CatalogStore
    from ai_edge.task_drafts.node import TaskDraftNode
    from robot_interfaces.srv import QueryTaskDraft, ReviewTaskDraft, SaveTaskDraft

    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False


@unittest.skipUnless(ROS_AVAILABLE, "需要 ROS 2 和构建后的 robot_interfaces")
class TaskDraftNodeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        database = Path(folder.name) / "drafts.sqlite3"
        catalog_path = Path(folder.name) / "catalog.sqlite3"
        CatalogStore(catalog_path).initialize(["station_a"], ["box_1"])
        rclpy.init(
            args=[
                "--ros-args",
                "-p",
                f"database_path:={database.as_posix()}",
                "-p",
                f"catalog_path:={catalog_path.as_posix()}",
            ]
        )
        self.addCleanup(rclpy.shutdown)
        # Constructing the real subclass exercises Node.handle and Node.services
        # during base initialization and entity creation. No executor is started.
        self.node = TaskDraftNode()
        self.addCleanup(self.node.destroy_node)
        self.addCleanup(self.node.store.close)
        self.addCleanup(self.node.timer.cancel)

    def stamp(self, request, identity):
        request.stamp = self.node.get_clock().now().to_msg()
        request.request_id = identity
        return request

    def test_constructor_preserves_node_handle_and_registers_all_draft_services(self):
        with self.node.handle:
            self.assertEqual(self.node.get_name(), "task_draft_node")
        names = {service.srv_name for service in self.node.services}
        self.assertTrue(
            {
                "/ai/task_drafts/save",
                "/ai/task_drafts/review",
                "/ai/task_drafts/query",
            }.issubset(names)
        )

    def test_save_query_and_review_keep_working_through_the_request_handler(self):
        request = self.stamp(SaveTaskDraft.Request(), "save-one")
        request.session_id, request.input_context_id = "session", "context"
        request.source_request_ids = ["audio-one"]
        request.candidate.stamp = request.stamp
        request.candidate.task_type = request.candidate.NAVIGATE
        request.candidate.destination_station_id = "station_a"
        saved = self.node.save(request, SaveTaskDraft.Response())
        self.assertTrue(saved.success, saved.message)
        self.assertEqual(saved.request_id, "save-one")
        self.assertEqual(saved.draft.state, saved.draft.EDITING)

        query = self.stamp(QueryTaskDraft.Request(), "query-one")
        query.draft_id = saved.draft.draft_id
        found = self.node.query(query, QueryTaskDraft.Response())
        self.assertTrue(found.success, found.message)
        self.assertEqual(len(found.drafts), 1)
        self.assertEqual(found.drafts[0].draft_id, saved.draft.draft_id)

        review = self.stamp(ReviewTaskDraft.Request(), "review-one")
        review.operation = review.CONFIRM
        for field in (
            "draft_id",
            "revision",
            "boot_id",
            "session_id",
            "input_context_id",
            "confirmation_token",
        ):
            setattr(review, field, getattr(saved.draft, field))
        confirmed = self.node.review(review, ReviewTaskDraft.Response())
        self.assertTrue(confirmed.success, confirmed.message)
        self.assertEqual(confirmed.draft.state, confirmed.draft.CONFIRMED)
        self.assertEqual(confirmed.draft.revision, saved.draft.revision)


if __name__ == "__main__":
    unittest.main()
