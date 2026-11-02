"""可重复验证模型输出边界，不依赖 ROS、Ollama 或模型下载。"""

import unittest

from ai_edge.semantic.ollama_backend import OllamaBackend
from ai_edge.semantic.semantic_contract import SemanticError, validate_candidate


class SemanticContractTests(unittest.TestCase):
    def setUp(self):
        self.stations = ["station_a", "station_b"]
        self.objects = ["box_1"]
        self.data = dict(
            task_type="NAVIGATE",
            object_id="",
            pickup_station_id="",
            dropoff_station_id="",
            destination_station_id="station_b",
            return_required=False,
            return_station_id="",
        )

    def parse(self, **changes):
        return validate_candidate(
            dict(self.data, **changes), self.stations, self.objects
        )

    def error(self, code, **changes):
        with self.assertRaises(SemanticError) as caught:
            self.parse(**changes)
        self.assertEqual(caught.exception.code, code)

    def test_navigation(self):
        self.assertEqual(self.parse().missing_fields, ())

    def test_transport_with_return(self):
        answer = self.parse(
            task_type="TRANSPORT",
            object_id="box_1",
            pickup_station_id="station_a",
            dropoff_station_id="station_b",
            destination_station_id="",
            return_required=True,
            return_station_id="station_a",
        )
        self.assertFalse(answer.missing_fields)

    def test_missing_destination(self):
        answer = self.parse(destination_station_id="")
        self.assertEqual(answer.missing_fields, ("destination_station_id",))
        self.assertIn("目标工位", answer.clarification_question)

    def test_missing_return(self):
        self.assertEqual(
            self.parse(return_required=True).missing_fields, ("return_station_id",)
        )

    def test_unknown_station(self):
        self.error("UNKNOWN_STATION", destination_station_id="station_x")

    def test_unknown_object(self):
        self.error(
            "UNKNOWN_OBJECT",
            task_type="TRANSPORT",
            object_id="secret",
            destination_station_id="",
        )

    def test_no_default_station(self):
        answer = validate_candidate(dict(self.data, destination_station_id=""), [], [])
        self.assertTrue(answer.missing_fields)

    def test_mutually_exclusive_fields(self):
        self.error("INVALID_MODEL_OUTPUT", pickup_station_id="station_a")

    def test_return_requires_flag(self):
        self.error("INVALID_MODEL_OUTPUT", return_station_id="station_a")

    def test_boolean_cannot_be_integer(self):
        self.error("INVALID_MODEL_OUTPUT", return_required=1)

    def test_no_extra_execution_permission(self):
        self.error("INVALID_MODEL_OUTPUT", execute=True)

    def test_missing_key_rejected(self):
        self.data.pop("return_required")
        self.error("INVALID_MODEL_OUTPUT")

    def test_unknown_is_not_motion(self):
        self.error("INVALID_MODEL_OUTPUT", task_type="UNKNOWN")
        self.assertTrue(
            self.parse(task_type="UNKNOWN", destination_station_id="").unsupported
        )

    def test_bad_types_rejected(self):
        for value in (None, 12, [], {}):
            with self.subTest(value=value):
                self.error("INVALID_MODEL_OUTPUT", destination_station_id=value)


class OllamaEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.backend = OllamaBackend()

    def test_unfinished_response(self):
        for response in (
            {"done": False},
            {"done": True, "done_reason": "length"},
            {"done": True, "message": {"content": "```json\n{}\n```"}},
            {"done": True, "message": {"content": None}},
            {"done": True},
        ):
            with self.subTest(response=response):
                self.backend.post = lambda *args, **kwargs: response
                with self.assertRaises(SemanticError) as caught:
                    self.backend.parse("去station_b", ["station_b"], [])
                self.assertEqual(caught.exception.code, "INVALID_MODEL_OUTPUT")

    def test_oversize_prompt_never_calls_model(self):
        def unexpected(*args, **kwargs):
            self.fail("超长请求不能进入模型调用")

        self.backend.post = unexpected
        with self.assertRaises(SemanticError) as caught:
            self.backend.parse("录" * 16384, ["station_b"], [])
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")


if __name__ == "__main__":
    unittest.main()
