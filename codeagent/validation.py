"""التحقق من الملفات بعد كتابتها مباشرة.

المبدأ: الوكيل لا يسلّم ملفاً لم يُفحص. كل كتابة تمرّ على فاحص حسب الامتداد،
والنتيجة تعود إلى النموذج نصاً صريحاً فيُجبر على الإصلاح قبل المتابعة.

الفحوص كلها بلا تبعيات إضافية: المكتبة القياسية + أدوات خارجية إن وُجدت.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

TIMEOUT = 25


@dataclass
class ValidationIssue:
    line: int
    message: str
    severity: str = "error"          # error | warning
    source: str = "builtin"

    def render(self) -> str:
        where = f"سطر {self.line}: " if self.line else ""
        mark = "❌" if self.severity == "error" else "⚠️"
        return f"{mark} {where}{self.message}"


@dataclass
class ValidationResult:
    path: str
    checked: bool = False
    checker: str = ""
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def render(self) -> str:
        if not self.checked:
            return ""
        if self.ok and not self.issues:
            return f"✅ التحقق ({self.checker}): الملف سليم."
        lines = [i.render() for i in self.issues[:12]]
        head = (
            f"❌ التحقق ({self.checker}) وجد {len(self.errors)} خطأ — أصلحها الآن قبل المتابعة:"
            if not self.ok
            else f"⚠️ التحقق ({self.checker}) — ملاحظات غير حاجبة:"
        )
        return head + "\n" + "\n".join(lines)


Checker = Callable[[str, Path], ValidationResult]


# ------------------------------------------------------------ فاحصات

def _python(text: str, path: Path) -> ValidationResult:
    res = ValidationResult(path=path.name, checked=True, checker="python")
    try:
        ast.parse(text, filename=path.name)
    except SyntaxError as exc:
        res.issues.append(ValidationIssue(exc.lineno or 0, f"خطأ نحوي: {exc.msg}"))
        return res

    # ruff إن كان مثبّتاً — يلتقط ما لا يلتقطه المحلل النحوي
    if shutil.which("ruff"):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        try:
            proc = subprocess.run(
                ["ruff", "check", "--output-format", "json", "--select", "E9,F", tmp_path],
                capture_output=True, text=True, timeout=TIMEOUT, check=False,
            )
            if proc.stdout.strip():
                for item in json.loads(proc.stdout):
                    res.issues.append(ValidationIssue(
                        (item.get("location") or {}).get("row", 0),
                        f"{item.get('code','')} {item.get('message','')}".strip(),
                        source="ruff",
                    ))
            res.checker = "python + ruff"
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            pass
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    return res


def _json_check(text: str, path: Path) -> ValidationResult:
    res = ValidationResult(path=path.name, checked=True, checker="json")
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        res.issues.append(ValidationIssue(exc.lineno, f"JSON غير صالح: {exc.msg}"))
    return res


def _yaml_check(text: str, path: Path) -> ValidationResult:
    res = ValidationResult(path=path.name, checked=True, checker="yaml")
    try:
        import yaml

        list(yaml.safe_load_all(text))
    except ImportError:
        res.checked = False
    except Exception as exc:  # noqa: BLE001 — yaml.YAMLError وفروعه
        line = getattr(getattr(exc, "problem_mark", None), "line", None)
        res.issues.append(ValidationIssue((line or 0) + 1, f"YAML غير صالح: {exc}"))
    return res


def _node_syntax(text: str, path: Path, kind: str) -> ValidationResult:
    """يستخدم node --check إن وُجد، وإلا يكتفي بفحص توازن الأقواس."""
    res = ValidationResult(path=path.name, checked=True, checker=kind)

    if kind == "javascript" and shutil.which("node") and path.suffix.lower() in (".js", ".cjs", ".mjs"):
        with tempfile.NamedTemporaryFile("w", suffix=path.suffix, delete=False, encoding="utf-8") as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        try:
            proc = subprocess.run(["node", "--check", tmp_path],
                                  capture_output=True, text=True, timeout=TIMEOUT, check=False)
            if proc.returncode != 0:
                msg = (proc.stderr or "").strip().splitlines()
                detail = next((l for l in msg if "SyntaxError" in l), msg[0] if msg else "خطأ نحوي")
                line = 0
                m = re.search(r":(\d+)", proc.stderr or "")
                if m:
                    line = int(m.group(1))
                res.issues.append(ValidationIssue(line, detail.strip(), source="node"))
            res.checker = "node --check"
            return res
        except (subprocess.SubprocessError, OSError):
            pass
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    issue = _balance(text)
    if issue:
        res.issues.append(issue)
    return res


def _balance(text: str) -> Optional[ValidationIssue]:
    """فحص توازن الأقواس مع تجاهل النصوص والتعليقات."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: List[tuple] = []
    line = 1
    i = 0
    in_str: Optional[str] = None
    in_line_comment = False
    in_block_comment = False

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if ch == "\n":
            line += 1
            in_line_comment = False
            i += 1
            continue

        if in_line_comment:
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in "\"'`":
            in_str = ch
            i += 1
            continue

        if ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            if not stack:
                return ValidationIssue(line, f"قوس إغلاق زائد: {ch}")
            opener, _ = stack.pop()
            if opener != pairs[ch]:
                return ValidationIssue(line, f"أقواس غير متطابقة: فُتح {opener} وأُغلق {ch}")
        i += 1

    if in_str:
        return ValidationIssue(line, "نص غير مغلق (علامة اقتباس ناقصة)")
    if stack:
        opener, opened_at = stack[-1]
        return ValidationIssue(opened_at, f"قوس {opener} لم يُغلق")
    return None


VOID_TAGS = {"area","base","br","col","embed","hr","img","input","link","meta","param",
             "source","track","wbr","!doctype","!--"}


def _html(text: str, path: Path) -> ValidationResult:
    res = ValidationResult(path=path.name, checked=True, checker="html")
    stripped = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    stripped = re.sub(r"<script\b.*?</script>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"<style\b.*?</style>", "", stripped, flags=re.DOTALL | re.IGNORECASE)

    stack: List[str] = []
    for match in re.finditer(r"<\s*(/?)\s*([a-zA-Z0-9!-]+)[^>]*?(/?)>", stripped):
        closing, tag, self_close = match.group(1), match.group(2).lower(), match.group(3)
        if tag in VOID_TAGS or self_close:
            continue
        if not closing:
            stack.append(tag)
        else:
            if not stack:
                res.issues.append(ValidationIssue(0, f"وسم إغلاق زائد: </{tag}>"))
                break
            if stack[-1] != tag:
                if tag in stack:
                    while stack and stack[-1] != tag:
                        res.issues.append(ValidationIssue(
                            0, f"الوسم <{stack.pop()}> لم يُغلق قبل </{tag}>", severity="warning"))
                    stack.pop() if stack else None
                else:
                    res.issues.append(ValidationIssue(0, f"</{tag}> بلا وسم مفتوح مطابق"))
                    break
            else:
                stack.pop()
    for tag in stack[:4]:
        res.issues.append(ValidationIssue(0, f"الوسم <{tag}> لم يُغلق"))

    if path.suffix.lower() in (".html", ".htm"):
        if "<html" in text.lower() and "lang=" not in text.lower():
            res.issues.append(ValidationIssue(0, "وسم <html> بلا سمة lang", severity="warning"))
        if re.search(r"<img\b(?![^>]*\balt=)", text, re.IGNORECASE):
            res.issues.append(ValidationIssue(0, "صورة بلا alt", severity="warning"))
    return res


def _css(text: str, path: Path) -> ValidationResult:
    res = ValidationResult(path=path.name, checked=True, checker="css")
    cleaned = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    if cleaned.count("{") != cleaned.count("}"):
        res.issues.append(ValidationIssue(
            0, f"أقواس CSS غير متوازنة: {cleaned.count('{')} فتح مقابل {cleaned.count('}')} إغلاق"))
    return res


def _markdown(text: str, path: Path) -> ValidationResult:
    res = ValidationResult(path=path.name, checked=True, checker="markdown")
    if text.count("```") % 2 != 0:
        res.issues.append(ValidationIssue(0, "كتلة كود ``` غير مغلقة"))
    if not re.search(r"^#\s+\S", text, re.MULTILINE):
        res.issues.append(ValidationIssue(0, "لا يوجد عنوان رئيسي (# )", severity="warning"))
    return res


CHECKERS: Dict[str, Checker] = {
    ".py": _python,
    ".json": _json_check,
    ".yaml": _yaml_check, ".yml": _yaml_check,
    ".js": lambda t, p: _node_syntax(t, p, "javascript"),
    ".mjs": lambda t, p: _node_syntax(t, p, "javascript"),
    ".cjs": lambda t, p: _node_syntax(t, p, "javascript"),
    ".jsx": lambda t, p: _node_syntax(t, p, "jsx"),
    ".ts": lambda t, p: _node_syntax(t, p, "typescript"),
    ".tsx": lambda t, p: _node_syntax(t, p, "tsx"),
    ".html": _html, ".htm": _html, ".vue": _html,
    ".css": _css, ".scss": _css,
    ".md": _markdown,
}


def validate(text: str, path: Path) -> ValidationResult:
    """يفحص المحتوى حسب امتداد المسار. الامتدادات غير المعروفة تمر بلا فحص."""
    checker = CHECKERS.get(path.suffix.lower())
    if checker is None:
        return ValidationResult(path=path.name, checked=False)
    try:
        return checker(text, path)
    except Exception as exc:  # noqa: BLE001 — عطل الفاحص لا يمنع الكتابة
        res = ValidationResult(path=path.name, checked=True, checker="internal")
        res.issues.append(ValidationIssue(0, f"تعذّر التحقق: {exc}", severity="warning"))
        return res


def supported_extensions() -> List[str]:
    return sorted(CHECKERS.keys())
