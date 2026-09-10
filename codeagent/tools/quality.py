"""أداة مراجعة الكود واكتشاف المشاكل."""
from __future__ import annotations

from typing import Any, Dict

from ..review import available_linters, run_review, summarize_issues
from ..validation import supported_extensions, validate
from ..safety import resolve_in_root
from .base import Tool, ToolContext, ToolResult, integer, obj, string


def _review_code(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    subpath = None
    if args.get("path"):
        subpath = resolve_in_root(ctx.cfg, args["path"])
        if not subpath.exists():
            return ToolResult.failure(f"المسار غير موجود: {args['path']}")

    limit = int(args.get("limit") or 60)
    issues = run_review(ctx.cfg, subpath=subpath, max_issues=max(limit, 1))
    counts = summarize_issues(issues)

    if not issues:
        return ToolResult.success("لم يُعثر على مشاكل وفق القواعد المدمجة.", counts=counts)

    lines = [issue.render() for issue in issues[:limit]]
    header = (
        f"عدد المشاكل: {len(issues)} "
        f"(عالية: {counts['high']}، متوسطة: {counts['medium']}، منخفضة: {counts['low']})"
    )
    linters = available_linters()
    footer = ("\nمحلّلات خارجية متاحة: " + ", ".join(linters)) if linters else ""
    return ToolResult.success(header + "\n" + "\n".join(lines) + footer, counts=counts)


def _validate_file(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    from ..safety import relative_path
    from ..walker import is_probably_binary

    path = resolve_in_root(ctx.cfg, args["path"])
    rel = relative_path(ctx.cfg, path)
    if not path.is_file():
        return ToolResult.failure(f"الملف غير موجود: {rel}")
    if is_probably_binary(path):
        return ToolResult.failure(f"'{rel}' ملف ثنائي — لا يُفحص نصياً.")

    result = validate(path.read_text(encoding="utf-8", errors="replace"), path)
    if not result.checked:
        return ToolResult.success(
            f"لا يوجد فاحص لامتداد '{path.suffix}'. المدعوم: {', '.join(supported_extensions())}"
        )
    return ToolResult(ok=result.ok, output=f"{rel}\n{result.render()}",
                      data={"ok": result.ok, "checker": result.checker})


TOOLS = [
    Tool(
        name="validate_file",
        description=(
            "تحقّق من سلامة ملف: أخطاء نحوية في Python وJavaScript وTypeScript، "
            "صحة JSON وYAML، توازن وسوم HTML وأقواس CSS، وكتل Markdown. "
            "استخدمها بعد أي تعديل يدوي وقبل إعلان انتهاء المهمة."
        ),
        parameters=obj({"path": string("مسار الملف")}, required=["path"]),
        handler=_validate_file,
    ),
    Tool(
        name="review_code",
        description=(
            "افحص المشروع أو مساراً محدداً بحثاً عن مشاكل: أسرار مكتوبة في الكود، ثغرات محتملة، "
            "أخطاء نحوية، دوال طويلة، وأسطر تصحيح متروكة."
        ),
        parameters=obj(
            {
                "path": string("مجلد أو ملف لحصر الفحص (اختياري)"),
                "limit": integer("أقصى عدد مشاكل تُعرض (افتراضي 60)"),
            }
        ),
        handler=_review_code,
    )
]
