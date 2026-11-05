"""ROS file precedence, validation and child-process parameter forwarding."""

import json
from pathlib import Path
import tempfile
import unittest

try:
    import rclpy
    from rclpy.context import Context
    from rclpy.node import Node
    import yaml
    from robot_visualization.config import DashboardConfig
    from robot_visualization.deployment import read_parameter_file
    from robot_visualization.runtime.supervisor import BackendSupervisor

    ROS_AVAILABLE = True
except ImportError:
    ROS_AVAILABLE = False


@unittest.skipUnless(ROS_AVAILABLE, "requires ROS 2 and built interfaces")
class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.context = Context()
        rclpy.init(context=self.context)
        self.addCleanup(self.context.shutdown)

    def node_config(self, overrides=None, files=()):
        args = ["--ros-args"]
        for path in files:
            args.extend(["--params-file", str(path)])
        for key, value in dict(data_root=str(self.root), **(overrides or {})).items():
            args.extend(["-p", key + ":=" + json.dumps(value)])
        node = Node(
            "config_test",
            context=self.context,
            cli_args=args,
            use_global_arguments=False,
        )
        self.addCleanup(node.destroy_node)
        return DashboardConfig.from_node(node)

    def test_layered_ros_files_then_cli_and_absolute_data_paths(self):
        config_dir = Path(__file__).resolve().parents[1] / "config"
        local = self.root / "override.yaml"
        local.write_text(
            yaml.safe_dump(
                {
                    "/**": {
                        "ros__parameters": {
                            "model": "local:4b",
                            "audio_root": "recordings/custom",
                            "request_timeout": 10.0,
                            "parse_timeout": 25.0,
                            "asr_ncpu": 2,
                            "asr_device": "cpu",
                            "num_ctx": 8192,
                            "num_predict": 1024,
                            "temperature": 0.2,
                            "robot_odom_topic": "/test/odom",
                            "robot_telemetry_timeout": 4.0,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        config = self.node_config(
            {"model": "cli:4b"},
            [
                config_dir / "common.yaml",
                config_dir / "simulation.yaml",
                local,
            ],
        )
        self.assertEqual(config.model, "cli:4b")
        self.assertTrue(config.use_simulator)
        self.assertEqual(config.audio_root, self.root / "recordings/custom")
        self.assertEqual(config.catalog_path, self.root / "task_data/catalog.sqlite3")
        self.assertEqual(
            config.draft_database_path, self.root / "task_data/task_drafts.sqlite3"
        )
        self.assertEqual(
            config.robot.journal_path,
            str(self.root / "task_data/simulation_dispatch.json"),
        )
        plans = dict(BackendSupervisor(config).plans())

        def parameters(name):
            command = plans[name]
            return dict(
                (
                    command[i + 1].split(":=", 1)[0],
                    json.loads(command[i + 1].split(":=", 1)[1]),
                )
                for i, value in enumerate(command)
                if value == "-p"
            )

        parser = parameters("parse_task_node")
        self.assertEqual(parser["model"], "cli:4b")
        self.assertEqual(parser["num_ctx"], 8192)
        self.assertEqual(parser["request_timeout"], 10.0)
        self.assertEqual(parser["temperature"], 0.2)
        self.assertEqual(parameters("asr_node")["ncpu"], 2)
        self.assertEqual(parameters("audio_processing_node")["parse_timeout"], 25.0)
        self.assertEqual(
            parameters("robot_state_query_node")["odom_topic"], "/test/odom"
        )
        self.assertEqual(parameters("robot_state_query_node")["max_data_age"], 4.0)
        self.assertEqual(
            parameters("task_draft_node")["database_path"],
            str(config.draft_database_path),
        )
        self.assertNotIn("allowed_station_ids", parameters("audio_processing_node"))

    def test_invalid_timeout_fails_before_creating_catalog(self):
        with self.assertRaisesRegex(ValueError, "parse_timeout"):
            self.node_config({"parse_timeout": 290.0})
        self.assertFalse((self.root / "task_data").exists())

    def test_clock_setting_reaches_managed_nodes_and_simulator(self):
        config = self.node_config({"use_sim_time": True, "use_simulator": True})
        self.assertTrue(config.use_sim_time)
        for name, command in BackendSupervisor(config).plans():
            if name == "robot_simulator":
                self.assertIn("use_sim_time:=true", command)
            else:
                self.assertIn("use_sim_time:=true", command)

    def test_external_file_typos_and_wrong_structure_fail(self):
        path = self.root / "bad.yaml"
        for document in (
            {"/**": {"ros__parameters": {"modle": "wrong"}}},
            {"parameters": {"model": "wrong"}},
        ):
            path.write_text(yaml.safe_dump(document), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_parameter_file(path)

    def test_packaged_examples_are_valid_and_real_mode_stays_real(self):
        config_dir = Path(__file__).resolve().parents[1] / "config"
        for path in config_dir.glob("*.yaml"):
            read_parameter_file(path)
        config = self.node_config(
            files=[config_dir / "common.yaml", config_dir / "real.yaml"]
        )
        self.assertFalse(config.use_simulator)
        self.assertNotIn("robot_simulator", dict(BackendSupervisor(config).plans()))


if __name__ == "__main__":
    unittest.main()
