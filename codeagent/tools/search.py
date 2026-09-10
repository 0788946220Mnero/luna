"""البحث داخل ملفات المشروع."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from ..safety import relative_path, resolve_in_root
from ..walker import is_probably_binary, iter_files
from .base import Tool, ToolContext, ToolResult, boolean, integer, obj, string


def search_project(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    query = args.get("query") or ""
    if not query.strip():
        return ToolResult.failure("نص البحث فارغ.")

    use_regex = bool(args.get("regex", False))
    case_sensitive = bool(args.get("case_sensitive", False))
    include = args.get("include")
    include_list: List[str] | None = None
    if include:
        include_list = [include] if isinstance(include, str) else list(include)
    limit = int(args.get("limit") or 80)

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        pattern = re.compile(query if use_regex else re.escape(query), flags)
    except re.error as exc:
        return ToolResult.failure(f"تعبير نمطي غير صالح: {exc}")

    subdir = None
    if args.get("path"):
        subdir = resolve_in_root(ctx.cfg, args["path"])

    results: List[str] = []
    files_hit = 0
    max_bytes = int(ctx.cfg.project.get("max_file_bytes", 400_000))

    for path in iter_files(ctx.cfg, subdir=subdir, include=include_list):
        if is_probably_binary(path):
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not pattern.search(text):
            continue
        files_hit += 1
        rel = relative_path(ctx.cfg, path)
        for idx, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                snippet = line.strip()
                if len(snippet) > 200:
                    snippet = snippet[:200] + " ..."
                results.append(f"{rel}:{idx}: {snippet}")
                if len(results) >= limit:
                    break
        if len(results) >= limit:
            break

    if not results:
        return ToolResult.success(f"لا نتائج للبحث عن '{query}'.", matches=0)

    header = f"{len(results)} نتيجة في {files_hit} ملف للبحث عن '{query}':\n"
    return ToolResult.success(header + "\n".join(results), matches=len(results), files=files_hit)


TOOLS = [
    Tool(
        name="search_project",
        description=(
            "ابحث عن نص أو تعبير نمطي داخل كل ملفات المشروع. يعيد المسار ورقم السطر والسطر نفسه. "
            "استخدمه لفهم أين تُستخدم دالة أو متغيّر قبل التعديل."
        ),
        parameters=obj(
            {
                "query": string("النص أو التعبير النمطي المراد البحث عنه"),
                "regex": boolean("اعتبار query تعبيراً نمطياً (افتراضي false)"),
                "case_sensitive": boolean("حساسية حالة الأحرف (افتراضي false)"),
                "include": string("نمط glob لتحديد الملفات مثل '*.py'"),
                "path": string("مجلد فرعي لحصر البحث فيه"),
                "limit": integer("أقصى عدد نتائج (افتراضي 80)"),
            },
            required=["query"],
        ),
        handler=search_project,
    )
]
