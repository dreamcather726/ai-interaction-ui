"""Read and validate deployment configuration without ROS, Qt or side effects."""

from pathlib import Path

import yaml

from robot_visualization.config import (
    parameter_names,
    parameter_types,
    validate_parameters,
)


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError("configuration keys must be strings")
            if key in mapping:
                raise ValueError(f"duplicate configuration key: {key}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def read_parameter_file(filename):
    path = Path(filename).expanduser().resolve()
    try:
        with path.open(encoding="utf-8") as stream:
            document = yaml.load(stream, Loader=UniqueKeyLoader)
        if not isinstance(document, dict) or set(document) != {"/**"}:
            raise ValueError("expected /**: ros__parameters: mapping")
        section = document["/**"]
        if not isinstance(section, dict) or set(section) != {"ros__parameters"}:
            raise ValueError("expected ros__parameters mapping")
        values = section["ros__parameters"]
        if not isinstance(values, dict):
            raise ValueError("parameters must be a mapping")
        unknown = set(values) - parameter_names()
        if unknown:
            raise ValueError(f"unknown dashboard parameters: {sorted(unknown)}")
        return path, validate_parameters(values, partial=True)
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def parse_override(name, text):
    kind = parameter_types()[name]
    if kind is str:
        return text
    try:
        value = yaml.safe_load(text)
        return validate_parameters({name: value}, partial=True)[name]
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid launch override {name}: {exc}") from exc


def load_configuration(filenames, overrides=None, *, cwd=None):
    """Merge layers, validate the result and resolve paths against one data root."""
    values, sources = {}, []
    for filename in filenames:
        path, layer = read_parameter_file(filename)
        sources.append(str(path))
        values.update(layer)
    overrides = overrides or {}
    values.update(overrides)
    values = validate_parameters(values)
    base = Path(cwd or Path.cwd()).expanduser().resolve()

    def absolute(value, root):
        path = Path(value).expanduser()
        return str((path if path.is_absolute() else root / path).resolve())

    values["data_root"] = absolute(values.get("data_root", "."), base)
    root = Path(values["data_root"])
    for name in (
        "audio_root",
        "catalog_path",
        "draft_database_path",
        "model_path",
        "robot_journal_path",
        "simulator_audio_file",
        "asr_python",
    ):
        value = values.get(name)
        if not value:
            continue
        if name == "asr_python" and "/" not in value and "\\" not in value:
            continue  # A command name is resolved on PATH by DashboardConfig.
        values[name] = absolute(value, root)
    values["config_sources"] = " < ".join(sources or ["code defaults"]) + (
        " < launch overrides" if overrides else ""
    )
    return values
