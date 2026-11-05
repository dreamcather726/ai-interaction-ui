"""Configuration errors fail before network requests or inference."""

import unittest
from unittest.mock import patch

from ai_edge.common.inference_config import (
    ASR_DEFAULTS,
    LLM_DEFAULTS,
    PIPELINE_DEFAULTS,
    validate_pipeline,
)
from ai_edge.semantic.ollama_backend import OllamaBackend


class InferenceConfigTests(unittest.TestCase):
    def test_invalid_budgets_and_types_are_rejected(self):
        defaults = dict(LLM_DEFAULTS, **ASR_DEFAULTS, **PIPELINE_DEFAULTS)
        validate_pipeline(defaults)
        for changed in (
            {"temperature": float("nan")},
            {"num_ctx": True},
            {"num_predict": 32768},
            {"asr_ncpu": 0},
            {"request_timeout": 301.0},
            {"asr_timeout": 180.0},
            {"parse_timeout": 299.0},
            {"ollama_url": "http://user:secret@localhost"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_pipeline(dict(defaults, **changed))

    def test_generation_uses_configured_model_and_sampling_options(self):
        backend = OllamaBackend(
            model="custom:4b",
            temperature=0.2,
            num_ctx=8192,
            num_predict=1024,
            keep_alive="10m",
        )
        with patch.object(
            backend, "post", return_value={"done": True, "message": {"content": "{}"}}
        ) as post:
            backend.generate([{"role": "user", "content": "hello"}], {})
        body = post.call_args.args[1]
        self.assertEqual(body["model"], "custom:4b")
        self.assertEqual(
            body["options"], dict(temperature=0.2, num_ctx=8192, num_predict=1024)
        )
        self.assertEqual(body["keep_alive"], "10m")


if __name__ == "__main__":
    unittest.main()
