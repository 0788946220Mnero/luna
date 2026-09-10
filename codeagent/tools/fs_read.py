"""أدوات قراءة الملفات وفهم بنية المشروع."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ..approvals import ApprovalRequest
from ..safety import SafetyError, is_sensitive, relative_path, resolve_in_root
from ..walker import analyze_project, is_probably_binary, iter_files
from .base import Tool, ToolContext, ToolResult, boolean, integer, obj, string


def _read_file(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    path = resolve_in_root(ctx.cfg, args["path"])
    rel = relative_path(ctx.cfg, path)

    if not path.exists():
        return ToolResult.failure(f"الملف غير موجود: {rel}")
    if path.is_dir():
        return ToolResult.failure(f"'{rel}' مجلد وليس ملفاً. استخدم list_dir.")
    if is_probably_binary(path):
        return ToolResult.failure(f"'{rel}' ملف ثنائي ولا يمكن قراءته كنص.")

    if is_sensitive(ctx.cfg, path) and ctx.cfg.safety.get("read_sensitive_requires_approval", True):
        ctx.approvals.require(
            ApprovalRequest(
                tool="read_file",
                summary=f"قراءة ملف حسّاس: {rel}",
                details="هذا الملف قد يحتوي مفاتيح أو كلمات مرور.",
                risk="high",
                targets=[rel],
            ),
            force=True,
        )

    max_bytes = int(ctx.cfg.project.get("max_file_bytes", 400_000))
    if path.stat().st_size > max_bytes * 4:
        return ToolResult.failure(f"'{rel}' أكبر من الحد المسموح ({path.stat().st_size} بايت).")

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(args.get("start_line") or 1))
    end = args.get("end_line")
    end = len(lines) if end in (None, 0) else min(int(end), len(lines))
    max_lines = int(ctx.cfg.project.get("max_read_lines", 1500))
    if end - start + 1 > max_lines:
        end = start + max_lines - 1

    selected = lines[start - 1:end]
    numbered = "\n".join(f"{i:>5} | {line}" for i, line in enumerate(selected, start=start))
    header = f"# {rel} (الأسطر {start}-{end} من {len(lines)})\n"
    return ToolResult.success(header + numbered, path=rel, total_lines=len(lines))


def _list_dir(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    target = resolve_in_root(ctx.cfg, args.get("path") or ".")
    rel = relative_path(ctx.cfg, target)
    if not target.exists():
        return ToolResult.failure(f"المسار غير موجود: {rel}")
    if not target.is_dir():
        return ToolResult.failure(f"'{rel}' ليس مجلداً.")

    ignore = set(ctx.cfg.ignore_names())
    rows = []
    for entry in sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name.lower())):
        if entry.name in ignore:
            continue
        if entry.is_dir():
            rows.append(f"[DIR ] {entry.name}/")
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            flag = " (حسّاس)" if is_sensitive(ctx.cfg, entry) else ""
            rows.append(f"[FILE] {entry.name} — {size} بايت{flag}")

    body = "\n".join(rows) if rows else "(المجلد فارغ أو كل محتوياته مستثناة)"
    return ToolResult.success(f"محتويات {rel or '.'}:\n{body}", count=len(rows))


def _project_structure(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    analysis = analyze_project(ctx.cfg)
    return ToolResult.success(analysis.to_text(), file_count=analysis.file_count)


def _find_files(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    pattern = args.get("pattern") or "*"
    limit = int(args.get("limit") or 100)
    matches = []
    for path in iter_files(ctx.cfg, include=[pattern]):
        matches.append(relative_path(ctx.cfg, path))
        if len(matches) >= limit:
            break
    if not matches:
        return ToolResult.success(f"لا توجد ملفات تطابق النمط '{pattern}'.", matches=[])
    return ToolResult.success(
        f"{len(matches)} ملف يطابق '{pattern}':\n" + "\n".join(matches), matches=matches
    )


TOOLS = [
    Tool(
        name="read_file",
        description="اقرأ محتوى ملف نصي داخل المشروع مع أرقام الأسطر. يدعم قراءة نطاق أسطر محدد.",
        parameters=obj(
            {
                "path": string("مسار الملف نسبةً لجذر المشروع"),
                "start_line": integer("أول سطر (اختياري، يبدأ من 1)"),
                "end_line": integer("آخر سطر (اختياري)"),
            },
            required=["path"],
        ),
        handler=_read_file,
    ),
    Tool(
        name="list_dir",
        description="اعرض محتويات مجلد داخل المشروع (ملفات ومجلدات مع الأحجام).",
        parameters=obj({"path": string("مسار المجلد، افتراضياً '.'")}),
        handler=_list_dir,
    ),
    Tool(
        name="project_structure",
        description="حلّل بنية المشروع كاملة: شجرة المجلدات، اللغات المستخدمة، ملفات الإعداد، ونقاط الدخول.",
        parameters=obj({}),
        handler=_project_structure,
    ),
    Tool(
        name="find_files",
        description="ابحث عن ملفات بالاسم أو بنمط glob مثل '*.py' أو 'src/**/*.ts'.",
        parameters=obj(
            {
                "pattern": string("نمط glob مثل '*.py'"),
                "limit": integer("أقصى عدد نتائج (افتراضي 100)"),
            },
            required=["pattern"],
        ),
        handler=_find_files,
    ),
]
