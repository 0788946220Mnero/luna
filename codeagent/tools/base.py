"""البنية الأساسية لنظام الأدوات."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..approvals import ApprovalManager
from ..backup import BackupManager
from ..config import Config
from ..store import Store


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, output: str, **data: Any) -> "ToolResult":
        return cls(ok=True, output=output, data=data)

    @classmethod
    def failure(cls, output: str, **data: Any) -> "ToolResult":
        return cls(ok=False, output=output, data=data)


@dataclass
class ToolContext:
    cfg: Config
    store: Store
    approvals: ApprovalManager
    backups: BackupManager
    op_id: Optional[int] = None       # يُملأ من المنفّذ قبل استدعاء الأداة
    emit: Callable[[str], None] = lambda msg: None


Handler = Callable[[ToolContext, Dict[str, Any]], ToolResult]


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Handler
    mutating: bool = False
    risk: str = "low"                 # low | medium | high

    def schema(self) -> Dict[str, Any]:
        """صيغة الأداة كما يتوقعها Ollama / OpenAI function calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def obj(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def string(desc: str) -> Dict[str, Any]:
    return {"type": "string", "description": desc}


def integer(desc: str) -> Dict[str, Any]:
    return {"type": "integer", "description": desc}


def boolean(desc: str) -> Dict[str, Any]:
    return {"type": "boolean", "description": desc}
