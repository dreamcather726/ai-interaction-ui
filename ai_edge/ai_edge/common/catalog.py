"""工位与物料基础资料；SQLite 版本校验避免多个窗口互相覆盖。"""

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path


def catalog_ids(snapshot):
    return tuple(
        [row["id"] for row in snapshot[kind] if row["enabled"]]
        for kind in ("stations", "objects")
    )


def seed_catalog(stations=(), objects=()):
    return dict(
        revision=0,
        **{
            kind: [
                dict(id=value, name=value, aliases=[], enabled=True) for value in ids
            ]
            for kind, ids in (("stations", stations), ("objects", objects))
        },
    )


def validate_catalog(snapshot):
    for kind in ("stations", "objects"):
        rows = snapshot[kind]
        if not isinstance(rows, list) or len(rows) > 128:
            raise ValueError("每类基础资料最多128条")
        names, identities = {}, set()
        for row in rows:
            if set(row) != {"id", "name", "aliases", "enabled"}:
                raise ValueError("资料字段不完整")
            if not isinstance(row["enabled"], bool):
                raise ValueError("启用状态无效")
            aliases = row["aliases"]
            if not isinstance(aliases, list) or len(aliases) > 8:
                raise ValueError("每条资料最多8个别名")
            for text in [row["id"], row["name"], *aliases]:
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or text != text.strip()
                    or len(text) > 128
                    or any(ord(char) < 32 for char in text)
                ):
                    raise ValueError(
                        "ID、名称和别名须为1～128字符，不能含首尾空格或控制字符"
                    )
            if row["id"] in identities:
                raise ValueError("同类资料的ID不能重复")
            identities.add(row["id"])
            for text in {row["id"], row["name"], *aliases}:
                key = text.casefold()
                if key in names and names[key] != row["id"]:
                    raise ValueError(f"名称或别名有歧义：{text}")
                names[key] = row["id"]
    return snapshot


class CatalogStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()

    def initialize(self, stations=(), objects=()):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        initial = validate_catalog(seed_catalog(stations, objects))
        with closing(sqlite3.connect(self.path, timeout=0.2)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS catalog "
                "(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER, body TEXT)"
            )
            db.execute(
                "INSERT OR IGNORE INTO catalog VALUES (1,1,?)",
                (json.dumps(initial, ensure_ascii=False),),
            )
            db.commit()
        return self.read()

    def read(self, stations=(), objects=()):
        # 独立节点没有资料库时兼容显式启动白名单；已有库不可退回陈旧参数。
        if not self.path.is_file():
            return validate_catalog(seed_catalog(stations, objects))
        with closing(
            sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
        ) as db:
            row = db.execute("SELECT revision,body FROM catalog WHERE id=1").fetchone()
        if row is None:
            raise ValueError("基础资料库缺少配置，请检查 catalog_path")
        value = validate_catalog(json.loads(row[1]))
        value["revision"] = row[0]
        return value

    def update(self, operation, kind, entity, expected_revision):
        if kind not in ("stations", "objects") or operation not in ("save", "delete"):
            raise ValueError("不支持的基础资料操作")
        with closing(
            sqlite3.connect(self.path, isolation_level=None, timeout=0.2)
        ) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                revision, body = db.execute(
                    "SELECT revision,body FROM catalog WHERE id=1"
                ).fetchone()
                if revision != expected_revision:
                    raise ValueError("基础资料已被其他窗口修改，请刷新后重试")
                value = json.loads(body)
                rows = value[kind]
                index = next(
                    (i for i, row in enumerate(rows) if row["id"] == entity["id"]), None
                )
                if operation == "delete":
                    if index is None:
                        raise ValueError("资料已经不存在，请刷新")
                    rows.pop(index)
                elif index is None:
                    rows.append(deepcopy(entity))
                else:
                    rows[index] = deepcopy(entity)
                validate_catalog(value)
                value["revision"] = revision + 1
                db.execute(
                    "UPDATE catalog SET revision=?,body=? WHERE id=1",
                    (revision + 1, json.dumps(value, ensure_ascii=False)),
                )
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        return value


def node_catalog(node):
    node.declare_parameter("catalog_path", "task_data/catalog.sqlite3")
    return CatalogStore(node.get_parameter("catalog_path").value)
