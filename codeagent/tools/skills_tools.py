"""أدوات المهارات: يقرأ الوكيل الإرشادات قبل أن يبدأ العمل."""
from __future__ import annotations

from typing import Any, Dict

from ..skills import get_registry
from .base import Tool, ToolContext, ToolResult, obj, string


def _list_skills(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    registry = get_registry(ctx.cfg.root)
    skills = registry.all()
    if not skills:
        return ToolResult.success("لا توجد مهارات متاحة.")

    task = args.get("task") or ""
    lines = []
    if task:
        matched = registry.match(task)
        if matched:
            lines.append("الأنسب لهذه المهمة:")
            lines += [f"  ★ {s.name}: {s.description}" for s in matched]
            lines.append("")
    lines.append("كل المهارات:")
    lines += [f"  {s.name} [{s.source}]: {s.description}" for s in skills]
    lines.append("\naqra المهارة كاملة بـ read_skill قبل أن تكتب أي ملف.")
    return ToolResult.success("\n".join(lines), count=len(skills))


def _read_skill(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    registry = get_registry(ctx.cfg.root)
    skill = registry.get(args["name"])
    if skill is None:
        available = ", ".join(s.name for s in registry.all())
        return ToolResult.failure(f"لا توجد مهارة باسم '{args['name']}'. المتاح: {available}")
    header = f"# مهارة: {skill.name}\n{skill.description}\n\n"
    return ToolResult.success(header + skill.body, name=skill.name, source=skill.source)


TOOLS = [
    Tool(
        name="list_skills",
        description=(
            "اعرض المهارات المتاحة (إرشادات مكتوبة لكل نوع عمل). "
            "مرّر وصف المهمة ليرشّح لك الأنسب. استدعِها في بداية أي مهمة تنتج ملفات."
        ),
        parameters=obj({"task": string("وصف المهمة لترشيح المهارات الأنسب (اختياري)")}),
        handler=_list_skills,
    ),
    Tool(
        name="read_skill",
        description=(
            "اقرأ مهارة كاملة: الأنماط الصحيحة، الأخطاء الشائعة، وقائمة التحقق. "
            "اقرأها **قبل** كتابة الملفات لا بعدها."
        ),
        parameters=obj({"name": string("اسم المهارة من list_skills")}, required=["name"]),
        handler=_read_skill,
    ),
]
