"""أدوات إنشاء وتعديل وحذف الملفات — كلها مرتبطة بـ diff ونسخة احتياطية وموافقة."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ..approvals import ApprovalRequest
from ..diffs import diff_stats, make_diff
from ..safety import is_sensitive, relative_path, resolve_in_root
from ..validation import validate
from ..walker import is_probably_binary
from .base import Tool, ToolContext, ToolResult, boolean, integer, obj, string


def _guard_sensitive(ctx: ToolContext, path: Path, action: str) -> None:
    if is_sensitive(ctx.cfg, path):
        ctx.approvals.require(
            ApprovalRequest(
                tool=action,
                summary=f"{action} على ملف حسّاس: {relative_path(ctx.cfg, path)}",
                details="هذا الملف مصنّف حسّاس (مفاتيح/إعدادات سرية).",
                risk="high",
                targets=[relative_path(ctx.cfg, path)],
            ),
            force=True,
        )


def _apply_write(
    ctx: ToolContext,
    path: Path,
    new_text: str,
    tool_name: str,
    summary: str,
) -> ToolResult:
    """المسار الموحّد لأي كتابة: diff → موافقة → نسخة احتياطية → كتابة."""
    rel = relative_path(ctx.cfg, path)
    old_text = ""
    if path.exists():
        if path.is_dir():
            return ToolResult.failure(f"'{rel}' مجلد وليس ملفاً.")
        old_text = path.read_text(encoding="utf-8", errors="replace")

    if old_text == new_text:
        return ToolResult.success(f"لا تغيير: محتوى '{rel}' مطابق بالفعل.", path=rel, changed=False)

    diff = make_diff(old_text, new_text, rel)
    added, removed = diff_stats(old_text, new_text)

    ctx.approvals.require(
        ApprovalRequest(
            tool=tool_name,
            summary=summary + f" (+{added}/-{removed})",
            details=diff,
            risk="medium",
            targets=[rel],
        )
    )

    if ctx.op_id is not None:
        ctx.backups.snapshot(ctx.op_id, path)

    # التحقق قبل الكتابة: الملف المعطوب لا يُحفظ في الوضع الصارم
    check = validate(new_text, path)
    strict = bool(ctx.cfg.data.get("validation", {}).get("strict", False))
    if check.checked and not check.ok and strict:
        ctx.emit(f"رُفضت كتابة {rel}: فشل التحقق")
        return ToolResult.failure(
            f"لم يُحفظ '{rel}' — الملف لم يجتز التحقق:\n{check.render()}\n"
            "صحّح المحتوى وأعد المحاولة."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")

    report = check.render()
    if check.checked and not check.ok:
        ctx.emit(f"⚠️ {rel}: فشل التحقق — يحتاج إصلاحاً")
        return ToolResult(
            ok=False,
            output=(
                f"كُتب '{rel}' (+{added}/-{removed}) لكنه **لم يجتز التحقق**.\n{report}\n"
                "أصلح هذه الأخطاء الآن بـ edit_file قبل أي خطوة أخرى، ولا تعتبر المهمة منتهية."
            ),
            data={"path": rel, "added": added, "removed": removed,
                  "validation_failed": True, "issues": [i.render() for i in check.issues]},
        )

    ctx.emit(f"تم كتابة {rel} (+{added}/-{removed})" + (" ✅" if check.checked else ""))
    message = f"تم حفظ '{rel}' بنجاح (+{added}/-{removed})."
    if report:
        message += "\n" + report
    return ToolResult.success(
        message, path=rel, added=added, removed=removed, diff=diff, changed=True,
        validated=check.checked,
    )


def _create_file(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    path = resolve_in_root(ctx.cfg, args["path"])
    rel = relative_path(ctx.cfg, path)
    overwrite = bool(args.get("overwrite", False))
    if path.exists() and not overwrite:
        return ToolResult.failure(
            f"الملف '{rel}' موجود مسبقاً. استخدم edit_file للتعديل أو overwrite=true للاستبدال."
        )
    _guard_sensitive(ctx, path, "create_file")
    content = args.get("content") or ""
    return _apply_write(ctx, path, content, "create_file", f"إنشاء الملف {rel}")


def _write_file(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    path = resolve_in_root(ctx.cfg, args["path"])
    rel = relative_path(ctx.cfg, path)
    _guard_sensitive(ctx, path, "write_file")
    return _apply_write(ctx, path, args.get("content") or "", "write_file", f"استبدال محتوى {rel}")


def _edit_file(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    path = resolve_in_root(ctx.cfg, args["path"])
    rel = relative_path(ctx.cfg, path)
    if not path.exists():
        return ToolResult.failure(f"الملف غير موجود: {rel}")
    if is_probably_binary(path):
        return ToolResult.failure(f"'{rel}' ملف ثنائي، لا يمكن تعديله نصياً.")
    _guard_sensitive(ctx, path, "edit_file")

    old_text = path.read_text(encoding="utf-8", errors="replace")
    target = args.get("old_text")
    if target is None or target == "":
        return ToolResult.failure("يجب تحديد old_text (النص المراد استبداله).")
    new_fragment = args.get("new_text")
    if new_fragment is None:
        return ToolResult.failure("يجب تحديد new_text (النص الجديد، ويمكن أن يكون فارغاً للحذف).")

    occurrences = old_text.count(target)
    if occurrences == 0:
        return ToolResult.failure(
            f"لم يُعثر على النص المطلوب في '{rel}'. تأكد من مطابقة المسافات والأسطر تماماً."
        )
    replace_all = bool(args.get("replace_all", False))
    if occurrences > 1 and not replace_all:
        return ToolResult.failure(
            f"النص مكرر {occurrences} مرة في '{rel}'. وسّع old_text ليصبح فريداً أو مرّر replace_all=true."
        )

    new_text = old_text.replace(target, new_fragment) if replace_all else old_text.replace(target, new_fragment, 1)
    return _apply_write(ctx, path, new_text, "edit_file", f"تعديل {rel}")


def _delete_file(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    path = resolve_in_root(ctx.cfg, args["path"])
    rel = relative_path(ctx.cfg, path)
    if not path.exists():
        return ToolResult.failure(f"الملف غير موجود: {rel}")
    if path.is_dir():
        return ToolResult.failure("حذف المجلدات غير مدعوم من الوكيل. احذف الملفات واحداً واحداً.")

    preview = ""
    if not is_probably_binary(path):
        text = path.read_text(encoding="utf-8", errors="replace")
        head = "\n".join(text.splitlines()[:20])
        preview = f"أول 20 سطراً:\n{head}"

    ctx.approvals.require(
        ApprovalRequest(
            tool="delete_file",
            summary=f"حذف الملف {rel}",
            details=preview,
            risk="high",
            targets=[rel],
        ),
        force=True,   # الحذف يتطلب تأكيداً صريحاً دائماً حتى مع auto_approve
    )

    if ctx.op_id is not None:
        ctx.backups.snapshot(ctx.op_id, path)
    path.unlink()
    ctx.emit(f"تم حذف {rel} (نسخة احتياطية محفوظة، يمكن التراجع بـ agent undo)")
    return ToolResult.success(f"تم حذف '{rel}'. يمكن استرجاعه عبر: agent undo", path=rel)


TOOLS = [
    Tool(
        name="create_file",
        description="أنشئ ملفاً جديداً بمحتوى محدد. يفشل إذا كان الملف موجوداً إلا إذا مُرّر overwrite=true.",
        parameters=obj(
            {
                "path": string("مسار الملف الجديد نسبةً لجذر المشروع"),
                "content": string("محتوى الملف الكامل"),
                "overwrite": boolean("استبدال الملف إن كان موجوداً (افتراضي false)"),
            },
            required=["path", "content"],
        ),
        handler=_create_file,
        mutating=True,
        risk="medium",
    ),
    Tool(
        name="write_file",
        description="استبدل محتوى ملف موجود بالكامل بمحتوى جديد. يعرض الفرق قبل الحفظ.",
        parameters=obj(
            {"path": string("مسار الملف"), "content": string("المحتوى الجديد الكامل")},
            required=["path", "content"],
        ),
        handler=_write_file,
        mutating=True,
        risk="medium",
    ),
    Tool(
        name="edit_file",
        description=(
            "عدّل جزءاً محدداً من ملف باستبدال نص بنص آخر. يجب أن يكون old_text فريداً داخل الملف "
            "ومطابقاً حرفياً (مسافات وأسطر). يعرض الفرق ويأخذ نسخة احتياطية قبل الحفظ."
        ),
        parameters=obj(
            {
                "path": string("مسار الملف"),
                "old_text": string("النص الحالي المراد استبداله (حرفياً)"),
                "new_text": string("النص الجديد"),
                "replace_all": boolean("استبدال كل التكرارات (افتراضي false)"),
            },
            required=["path", "old_text", "new_text"],
        ),
        handler=_edit_file,
        mutating=True,
        risk="medium",
    ),
    Tool(
        name="delete_file",
        description="احذف ملفاً. يتطلب تأكيداً صريحاً من المستخدم دائماً، ويأخذ نسخة احتياطية قابلة للاسترجاع.",
        parameters=obj({"path": string("مسار الملف المراد حذفه")}, required=["path"]),
        handler=_delete_file,
        mutating=True,
        risk="high",
    ),
]
