"""Offline configuration rules and backend handoff; no ROS, Qt or models."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from robot_visualization.config import DashboardConfig, validate_parameters
from robot_visualization.configuration import (
    load_configuration,
    parse_override,
    read_parameter_file,
)
from robot_visualization.runtime.supervisor import BackendSupervisor


class ConfigurationRulesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.config_dir = Path(__file__).resolve().parents[1] / "config"

    def write_config(self, values):
        path = self.root / "local.yaml"
        path.write_text(
            yaml.safe_dump({"/**": {"ros__parameters": values}}), encoding="utf-8"
        )
        return path

    def test_layers_cli_and_paths_have_one_precedence_and_root(self):
        local = self.write_config(
            {
                "model": "local:4b",
                "data_root": "deployment",
                "audio_root": "recordings/custom",
                "model_path": "models/asr",
                "asr_python": "venv/bin/python",
                "simulator_audio_file": "input.wav",
                "request_timeout": 10,
                "parse_timeout": 25,
                "robot_query_timeout": 3,
            }
        )
        values = load_configuration(
            [
                self.config_dir / "common.yaml",
                self.config_dir / "simulation.yaml",
                local,
            ],
            {"model": "cli:4b", "data_root": str(self.root / "data")},
            cwd=self.root / "another-launch-directory",
        )
        self.assertEqual(values["model"], "cli:4b")
        self.assertTrue(values["use_simulator"])
        for name, relative in {
            "audio_root": "recordings/custom",
            "catalog_path": "task_data/catalog.sqlite3",
            "draft_database_path": "task_data/task_drafts.sqlite3",
            "model_path": "models/asr",
            "asr_python": "venv/bin/python",
            "simulator_audio_file": "input.wav",
            "robot_journal_path": "task_data/simulation_dispatch.json",
        }.items():
            self.assertEqual(values[name], str(self.root / "data" / relative))
        self.assertIs(type(values["request_timeout"]), float)
        self.assertIn(str(local), values["config_sources"])
        self.assertTrue(values["config_sources"].endswith("launch overrides"))
        self.assertFalse((self.root / "data").exists())

    def test_relative_data_root_and_path_command(self):
        values = load_configuration(
            [], {"data_root": "deploy", "asr_python": "python3"}, cwd=self.root
        )
        self.assertEqual(values["data_root"], str(self.root / "deploy"))
        self.assertEqual(values["asr_python"], "python3")

    def test_typed_overrides_preserve_strings_and_reject_coercion(self):
        self.assertEqual(parse_override("model", "true"), "true")
        self.assertFalse(parse_override("start_microphone", "false"))
        self.assertEqual(parse_override("num_ctx", "8192"), 8192)
        self.assertIs(type(parse_override("request_timeout", "10")), float)
        for name, value in (
            ("num_ctx", "true"),
            ("use_sim_time", "1"),
            ("asr_ncpu", "2.5"),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                parse_override(name, value)

    def test_bad_files_fail_with_source_and_parameter(self):
        path = self.root / "bad.yaml"
        for content, error in (
            (
                "/**:\n  ros__parameters:\n    model: first\n    model: second\n",
                "duplicate",
            ),
            (yaml.safe_dump({"/**": {"ros__parameters": {"modle": "wrong"}}}), "modle"),
            (
                yaml.safe_dump(
                    {"/**": {"ros__parameters": {"start_backend": "false"}}}
                ),
                "start_backend",
            ),
            (
                yaml.safe_dump(
                    {"/**": {"ros__parameters": {"robot_telemetry_timeout": True}}}
                ),
                "robot_telemetry_timeout",
            ),
            (
                yaml.safe_dump(
                    {"/**": {"ros__parameters": {"allowed_station_ids": [1]}}}
                ),
                "allowed_station_ids",
            ),
            ("/**: {}", "ros__parameters"),
        ):
            with self.subTest(error=error):
                path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, error) as caught:
                    read_parameter_file(path)
                self.assertIn(str(path), str(caught.exception))

    def test_final_timeout_relationship_is_checked_after_merging(self):
        path = self.write_config({"request_timeout": 10.0})
        with self.assertRaisesRegex(ValueError, "parse_timeout"):
            load_configuration([path], {"parse_timeout": 15.0})
        self.assertEqual(
            load_configuration([path], {"parse_timeout": 25.0})["parse_timeout"], 25.0
        )
        for values in (
            {"temperature": float("nan")},
            {"num_ctx": True},
            {"audio_root": ""},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_parameters(values)

    def test_real_mode_and_packaged_examples(self):
        for path in self.config_dir.glob("*.yaml"):
            read_parameter_file(path)
        values = load_configuration(
            [self.config_dir / "common.yaml", self.config_dir / "real.yaml"]
        )
        self.assertFalse(values["use_simulator"])

    def dashboard(self, **overrides):
        return DashboardConfig(
            audio_root=self.root / "audio",
            model_path=str(self.root / "model"),
            asr_python=sys.executable,
            model="custom:4b",
            ollama_url="http://127.0.0.1:11434",
            device="@DEFAULT_SOURCE@",
            stations=[],
            objects=[],
            **overrides,
        )

    def test_changed_inference_settings_reach_correct_backend_parameters(self):
        config = self.dashboard(
            temperature=0.2,
            num_ctx=8192,
            num_predict=1024,
            asr_ncpu=2,
            asr_recognition_timeout=10.0,
            asr_timeout=15.0,
            request_timeout=10.0,
            parse_timeout=25.0,
            draft_database_path=self.root / "drafts.sqlite3",
        )
        plans = dict(BackendSupervisor(config).plans())

        def params(node):
            command = plans[node]
            return dict(
                command[i + 1].split(":=", 1)
                for i, value in enumerate(command)
                if value == "-p"
            )

        self.assertEqual(json.loads(params("parse_task_node")["model"]), "custom:4b")
        self.assertEqual(json.loads(params("parse_task_node")["num_ctx"]), 8192)
        self.assertEqual(json.loads(params("parse_task_node")["temperature"]), 0.2)
        self.assertEqual(json.loads(params("asr_node")["ncpu"]), 2)
        self.assertEqual(json.loads(params("asr_node")["recognition_timeout"]), 10.0)
        self.assertEqual(
            json.loads(params("audio_processing_node")["asr_timeout"]), 15.0
        )
        self.assertEqual(
            json.loads(params("audio_processing_node")["parse_timeout"]), 25.0
        )
        self.assertEqual(
            json.loads(params("task_draft_node")["database_path"]),
            str(config.draft_database_path),
        )

    def test_connection_only_startup_persists_configuration_and_runtime(self):
        manager = BackendSupervisor(
            self.dashboard(start_backend=False, config_sources="common < real < local")
        )
        self.addCleanup(manager.close)
        with (
            patch("pathlib.Path.home", return_value=self.root),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            manager.start(set())
        content = Path(manager.log_file.name).read_text(encoding="utf-8")
        self.assertIn("common < real < local", content)
        self.assertIn('"deployment_mode": "real"', content)
        self.assertIn("interface_package_version", content)
        self.assertIn("code_paths", content)
        self.assertEqual(manager.children, {})

    def test_external_node_warning_does_not_claim_configuration_was_applied(self):
        manager = BackendSupervisor(self.dashboard())
        self.addCleanup(manager.close)
        with (
            patch("pathlib.Path.home", return_value=self.root),
            patch.object(manager, "ensure_ollama"),
            patch.object(
                manager, "plans", return_value=[("asr_node", ["not-executed"])]
            ),
            patch.object(manager, "spawn") as spawn,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            manager.start({"asr_node"})
        spawn.assert_not_called()
        self.assertEqual(manager.external_nodes, {"asr_node"})
        self.assertIn(
            "本次配置未应用", Path(manager.log_file.name).read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
