"""أدوات Git للقراءة فقط: diff / status / log."""
from __future__ import annotations

import shutil
import subprocess
from typing import Any, Dict, List

from ..backup import git_available, run_git
from ..safety import relative_path, resolve_in_root
from .base import Tool, ToolContext, ToolResult, boolean, integer, obj, string

MAX_DIFF = 20_000


def _require_git(ctx: ToolContext) -> str | None:
    if shutil.which("git") is None:
        return "Git غير مثبّت على هذا الجهاز."
    if not (ctx.cfg.root / ".git").exists():
        return f"المجلد '{ctx.cfg.root}' ليس مستودع Git. شغّل: git init"
    return None


def _git_diff(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    error = _require_git(ctx)
    if error:
        return ToolResult.failure(error)

    argv: List[str] = ["diff"]
    if args.get("staged"):
        argv.append("--staged")
    if args.get("stat"):
        argv.append("--stat")
    if args.get("path"):
        target = resolve_in_root(ctx.cfg, args["path"])
        argv += ["--", relative_path(ctx.cfg, target)]

    try:
        proc = run_git(ctx.cfg, argv, timeout=45)
    except subprocess.SubprocessError as exc:
        return ToolResult.failure(f"فشل تنفيذ git: {exc}")

    if proc.returncode != 0:
        return ToolResult.failure(f"git diff فشل: {proc.stderr.strip()}")
    output = proc.stdout.strip()
    if not output:
        return ToolResult.success("لا توجد تغييرات غير مُلتزَمة (working tree نظيف).")
    if len(output) > MAX_DIFF:
        output = output[:MAX_DIFF] + "\n... [تم اقتطاع الفرق]"
    return ToolResult.success(output)


def _git_status(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    error = _require_git(ctx)
    if error:
        return ToolResult.failure(error)
    proc = run_git(ctx.cfg, ["status", "--short", "--branch"], timeout=30)
    if proc.returncode != 0:
        return ToolResult.failure(f"git status فشل: {proc.stderr.strip()}")
    return ToolResult.success(proc.stdout.strip() or "لا تغييرات.")


def _git_log(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    error = _require_git(ctx)
    if error:
        return ToolResult.failure(error)
    limit = str(int(args.get("limit") or 10))
    proc = run_git(ctx.cfg, ["log", f"-{limit}", "--oneline", "--decorate"], timeout=30)
    if proc.returncode != 0:
        return ToolResult.failure(f"git log فشل: {proc.stderr.strip()}")
    return ToolResult.success(proc.stdout.strip() or "لا توجد التزامات بعد.")


TOOLS = [
    Tool(
        name="git_diff",
        description="اعرض التغييرات غير المُلتزَمة في المستودع (يمكن حصرها بملف معيّن).",
        parameters=obj(
            {
                "path": string("مسار ملف لحصر الفرق (اختياري)"),
                "staged": boolean("عرض التغييرات المُجهّزة للالتزام"),
                "stat": boolean("عرض ملخّص إحصائي فقط"),
            }
        ),
        handler=_git_diff,
    ),
    Tool(
        name="git_status",
        description="اعرض حالة مستودع Git: الفرع والملفات المعدّلة وغير المتتبَّعة.",
        parameters=obj({}),
        handler=_git_status,
    ),
    Tool(
        name="git_log",
        description="اعرض آخر الالتزامات (commits) في المستودع.",
        parameters=obj({"limit": integer("عدد الالتزامات (افتراضي 10)")}),
        handler=_git_log,
    ),
]
