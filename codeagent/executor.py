"""البوابة الوحيدة لتنفيذ الأدوات: تسجيل + صلاحيات + نسخ احتياطي + معالجة أخطاء."""
from __future__ import annotations

import logging
import traceback
from typing import Any, Dict

from .approvals import ApprovalDenied, ApprovalManager
from .backup import BackupManager
from .config import Config
from .safety import SafetyError
from .store import Store
from .tools import REGISTRY, ToolContext, ToolResult

logger = logging.getLogger("codeagent")


class Executor:
    """ينفّذ الأدوات بعد المرور بكل طبقات الحماية ويسجّل كل شيء."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        approvals: ApprovalManager,
        emit=None,
    ):
        self.cfg = cfg
        self.store = store
        self.approvals = approvals
        self.backups = BackupManager(cfg, store)
        self.emit = emit or (lambda msg: None)

    def run(self, name: str, args: Dict[str, Any]) -> ToolResult:
        tool = REGISTRY.get(name)
        if tool is None:
            available = ", ".join(sorted(REGISTRY))
            self.store.log_operation(name, args, "error", "أداة غير معروفة")
            return ToolResult.failure(f"أداة غير معروفة '{name}'. الأدوات المتاحة: {available}")

        if not isinstance(args, dict):
            return ToolResult.failure("وسائط الأداة يجب أن تكون كائن JSON.")

        # تحقق من الوسائط المطلوبة قبل التنفيذ
        required = tool.parameters.get("required", [])
        missing = [key for key in required if args.get(key) in (None, "")]
        if missing:
            msg = f"وسائط ناقصة للأداة '{name}': {', '.join(missing)}"
            self.store.log_operation(name, args, "error", msg)
            return ToolResult.failure(msg)

        op_id = self.store.log_operation(name, args, "started")
        ctx = ToolContext(
            cfg=self.cfg,
            store=self.store,
            approvals=self.approvals,
            backups=self.backups,
            op_id=op_id,
            emit=self.emit,
        )

        try:
            result = tool.handler(ctx, args)
        except ApprovalDenied as exc:
            self.store.update_operation(op_id, "denied", str(exc))
            logger.info("رُفضت العملية %s: %s", name, exc)
            return ToolResult.failure(f"العملية أُلغيت من المستخدم: {exc}")
        except SafetyError as exc:
            self.store.update_operation(op_id, "blocked", str(exc))
            logger.warning("حُظرت العملية %s: %s", name, exc)
            return ToolResult.failure(f"حظر أمني: {exc}")
        except FileNotFoundError as exc:
            self.store.update_operation(op_id, "error", str(exc))
            return ToolResult.failure(f"ملف غير موجود: {exc}")
        except PermissionError as exc:
            self.store.update_operation(op_id, "error", str(exc))
            return ToolResult.failure(f"لا توجد صلاحية على النظام: {exc}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self.store.update_operation(op_id, "error", detail)
            logger.error("فشل تنفيذ %s: %s\n%s", name, detail, traceback.format_exc())
            return ToolResult.failure(f"فشل تنفيذ '{name}': {detail}")

        status = "ok" if result.ok else "failed"
        self.store.update_operation(op_id, status, result.output[:2000])
        return result
