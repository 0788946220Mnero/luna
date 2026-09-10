"""نظام الصلاحيات والموافقات."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .config import Config


class ApprovalDenied(Exception):
    """يُرفع عندما يرفض المستخدم عملية."""


@dataclass
class ApprovalRequest:
    tool: str
    summary: str
    details: str = ""
    risk: str = "medium"          # low | medium | high
    targets: List[str] = field(default_factory=list)


Prompter = Callable[[ApprovalRequest], bool]


def cli_prompter(request: ApprovalRequest) -> bool:
    """موافقة عبر سطر الأوامر (تُستبدل من CLI بواجهة rich)."""
    print(f"\n[موافقة مطلوبة] {request.tool} — {request.summary}")
    if request.details:
        print(request.details)
    answer = input("تنفيذ؟ [y/N]: ").strip().lower()
    return answer in {"y", "yes", "ن", "نعم"}


class ApprovalManager:
    def __init__(
        self,
        cfg: Config,
        prompter: Optional[Prompter] = None,
        auto_approve: Optional[bool] = None,
        deny_all: bool = False,
    ):
        self.cfg = cfg
        self.prompter = prompter or cli_prompter
        self.auto_approve = (
            cfg.safety.get("auto_approve", False) if auto_approve is None else auto_approve
        )
        self.deny_all = deny_all
        self.session_grants: Dict[str, bool] = {}
        self.decisions: List[tuple] = []

    def requires_approval(self, tool: str) -> bool:
        return tool in set(self.cfg.safety.get("require_approval_for", []))

    def request(self, req: ApprovalRequest, force: bool = False) -> bool:
        """يطلب الموافقة. force=True يتجاوز auto_approve للعمليات عالية الخطورة."""
        if self.deny_all:
            self.decisions.append((req.tool, "denied-mode"))
            return False

        if self.auto_approve and not force and req.risk != "high":
            self.decisions.append((req.tool, "auto"))
            return True

        approved = bool(self.prompter(req))
        self.decisions.append((req.tool, "yes" if approved else "no"))
        return approved

    def require(self, req: ApprovalRequest, force: bool = False) -> None:
        if not self.request(req, force=force):
            raise ApprovalDenied(f"رفض المستخدم العملية: {req.tool} — {req.summary}")
