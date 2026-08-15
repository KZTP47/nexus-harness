from __future__ import annotations

import importlib.metadata
import importlib.util
from pathlib import Path
from typing import Any, Protocol

from .config import LoadedConfig
from .models import HarnessError
from .safety import confined_path


class HarnessPlugin(Protocol):
    name: str

    def register(self, registry: "PluginRegistry") -> None: ...


class PluginRegistry:
    def __init__(self) -> None:
        self.detectors: list[Any] = []
        self.workflow_nodes: dict[str, Any] = {}
        self.doctor_checks: list[Any] = []

    def add_detector(self, detector: Any) -> None:
        self.detectors.append(detector)

    def add_workflow_node(self, name: str, node: Any) -> None:
        if name in self.workflow_nodes:
            raise HarnessError(f"Workflow node is already registered: {name}")
        self.workflow_nodes[name] = node

    def add_doctor_check(self, check: Any) -> None:
        self.doctor_checks.append(check)


def load_plugins(config: LoadedConfig) -> PluginRegistry:
    registry = PluginRegistry()
    enabled = set(config.get("plugins.enabled", []))
    for entry in importlib.metadata.entry_points(group="our_harness.plugins"):
        if entry.name in enabled:
            plugin = entry.load()()
            plugin.register(registry)
    for configured in config.get("plugins.paths", []):
        path = confined_path(config.project_root, configured, allow_missing=False)
        if path.stem not in enabled:
            continue
        spec = importlib.util.spec_from_file_location(f"our_harness_user_plugin_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise HarnessError(f"Cannot load plugin: {configured}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin = module.plugin()
        plugin.register(registry)
    return registry
