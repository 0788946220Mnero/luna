"""سجل الأدوات المتاحة للوكيل."""
from __future__ import annotations

from typing import Dict, List

from . import (
    fs_read, fs_write, git_tools, quality, search, shell, skills_tools, tests, vision_tools,
)
from .base import Tool, ToolContext, ToolResult

_MODULES = [fs_read, fs_write, search, shell, tests, git_tools, quality, vision_tools, skills_tools]

REGISTRY: Dict[str, Tool] = {}
for module in _MODULES:
    for tool in getattr(module, "TOOLS", []):
        if tool.name in REGISTRY:
            raise RuntimeError(f"اسم أداة مكرّر: {tool.name}")
        REGISTRY[tool.name] = tool


def get_tool(name: str) -> Tool | None:
    return REGISTRY.get(name)


def all_tools() -> List[Tool]:
    return list(REGISTRY.values())


def tool_schemas() -> List[dict]:
    return [tool.schema() for tool in REGISTRY.values()]


__all__ = [
    "REGISTRY", "Tool", "ToolContext", "ToolResult",
    "get_tool", "all_tools", "tool_schemas",
]
