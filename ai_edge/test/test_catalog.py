"""基础资料版本、别名冲突、停用和提示词数据回归；由用户自行运行。"""

import json
import tempfile
import unittest
from pathlib import Path

from ai_edge.common.catalog import CatalogStore, catalog_ids
from ai_edge.semantic.semantic_prompt import build_messages


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = CatalogStore(Path(self.directory.name) / "catalog.sqlite3")
        self.initial = self.store.initialize(["station_a"], ["box_1"])

    def entity(self, **changes):
        return dict(
            dict(id="station_b", name="收货区", aliases=["B点"], enabled=True),
            **changes,
        )

    def test_two_windows_cannot_overwrite_newer_revision(self):
        first = self.store.update(
            "save", "stations", self.entity(), self.initial["revision"]
        )
        with self.assertRaisesRegex(ValueError, "其他窗口"):
            self.store.update(
                "delete", "objects", {"id": "box_1"}, self.initial["revision"]
            )
        self.assertEqual(self.store.read(), first)

    def test_ambiguous_alias_rolls_back_without_overwriting_previous_rows(self):
        first = self.store.update(
            "save", "stations", self.entity(), self.initial["revision"]
        )
        with self.assertRaisesRegex(ValueError, "歧义"):
            self.store.update(
                "save",
                "stations",
                self.entity(id="station_c", name="发货区"),
                first["revision"],
            )
        self.assertEqual(self.store.read(), first)

    def test_disabled_entities_leave_whitelist_but_keep_their_metadata(self):
        value = self.store.update(
            "save",
            "objects",
            dict(id="box_1", name="一号箱子", aliases=[], enabled=False),
            self.initial["revision"],
        )
        self.assertEqual(catalog_ids(value), (["station_a"], []))
        self.assertEqual(value["objects"][0]["name"], "一号箱子")
        self.assertEqual(self.store.initialize(["old_station"], ["old_box"]), value)

    def test_configured_aliases_are_request_data_not_system_instructions(self):
        value = self.store.update(
            "save", "stations", self.entity(), self.initial["revision"]
        )
        messages = build_messages("去B点", ["station_b"], [], [], None, value)
        data = json.loads(messages[-1]["content"])
        self.assertEqual(data["configured_entities"]["stations"][1]["aliases"], ["B点"])
        self.assertNotIn("收货区", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
