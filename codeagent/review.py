"""فحص جودة الكود: قواعد ثابتة + محلّلات خارجية إن وُجدت."""
from __future__ import annotations

import ast
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .config import LANGUAGE_MAP, Config
from .safety import relative_path
from .walker import is_probably_binary, iter_files

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Issue:
    path: str
    line: int
    severity: str
    rule: str
    message: str

    def render(self) -> str:
        return f"[{self.severity.upper():6}] {self.path}:{self.line} ({self.rule}) — {self.message}"


SECRET_PATTERNS = [
    (re.compile(r"""(?i)(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['"][^'"]{8,}['"]"""),
     "سر مكتوب داخل الكود (hardcoded secret)"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "مفتاح API يبدو مكشوفاً"),
    (re.compile(r"(?i)AKIA[0-9A-Z]{16}"), "مفتاح AWS مكشوف"),
]

GENERIC_PATTERNS = [
    (re.compile(r"\b(TODO|FIXME|HACK|XXX)\b"), "low", "todo", "ملاحظة معلّقة في الكود"),
    (re.compile(r"\beval\s*\("), "high", "dangerous-eval", "استخدام eval قد يسبب ثغرة تنفيذ كود"),
    (re.compile(r"\bexec\s*\("), "high", "dangerous-exec", "استخدام exec خطير"),
    (re.compile(r"\bselect\b.*\+\s*\w+|execute\(\s*f?['\"].*%s.*\+", re.IGNORECASE),
     "high", "sql-injection", "بناء استعلام SQL بدمج نصوص — استخدم معاملات مُهيّأة"),
    (re.compile(r"innerHTML\s*="), "medium", "xss", "الكتابة في innerHTML قد تسبب XSS"),
    (re.compile(r"verify\s*=\s*False|rejectUnauthorized:\s*false"),
     "high", "tls-off", "تعطيل التحقق من شهادات TLS"),
    (re.compile(r"\bconsole\.log\("), "low", "debug-log", "سطر تصحيح متروك"),
    (re.compile(r"^\s*print\(", re.MULTILINE), "low", "debug-print", "استدعاء print قد يكون تصحيحاً متروكاً"),
]


def _python_ast_issues(rel: str, text: str) -> List[Issue]:
    issues: List[Issue] = []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [Issue(rel, exc.lineno or 1, "high", "syntax-error", f"خطأ نحوي: {exc.msg}")]

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            issues.append(Issue(rel, node.lineno, "medium", "bare-except",
                                "except عام يبتلع كل الأخطاء — حدّد نوع الاستثناء"))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_lines = getattr(node, "end_lineno", node.lineno) - node.lineno
            if body_lines > 80:
                issues.append(Issue(rel, node.lineno, "low", "long-function",
                                    f"الدالة '{node.name}' طويلة ({body_lines} سطر) — فكّر بتقسيمها"))
            args = node.args
            total_args = len(args.args) + len(args.kwonlyargs)
            if total_args > 7:
                issues.append(Issue(rel, node.lineno, "low", "too-many-args",
                                    f"الدالة '{node.name}' تأخذ {total_args} وسيطاً"))
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name == "run" and any(
                isinstance(kw.value, ast.Constant) and kw.arg == "shell" and kw.value.value is True
                for kw in node.keywords
            ):
                issues.append(Issue(rel, node.lineno, "medium", "shell-true",
                                    "subprocess.run(shell=True) — تأكد من تعقيم المدخلات"))
    return issues


def scan_text(cfg: Config, rel: str, text: str, language: Optional[str]) -> List[Issue]:
    issues: List[Issue] = []
    lines = text.splitlines()

    for pattern, message in SECRET_PATTERNS:
        for idx, line in enumerate(lines, start=1):
            if pattern.search(line):
                issues.append(Issue(rel, idx, "high", "secret", message))

    for pattern, severity, rule, message in GENERIC_PATTERNS:
        if rule == "debug-print" and language != "Python":
            continue
        if rule == "debug-log" and language not in {"JavaScript", "TypeScript", "Vue"}:
            continue
        for idx, line in enumerate(lines, start=1):
            if pattern.search(line):
                issues.append(Issue(rel, idx, severity, rule, message))

    if len(lines) > 600:
        issues.append(Issue(rel, 1, "low", "large-file",
                            f"الملف كبير ({len(lines)} سطر) — يُفضّل تقسيمه"))

    for idx, line in enumerate(lines, start=1):
        if len(line) > 200:
            issues.append(Issue(rel, idx, "low", "long-line", f"سطر طويل جداً ({len(line)} حرفاً)"))
            break

    if language == "Python":
        issues.extend(_python_ast_issues(rel, text))

    return issues


def run_review(cfg: Config, subpath: Optional[Path] = None, max_issues: int = 200) -> List[Issue]:
    issues: List[Issue] = []
    max_bytes = int(cfg.project.get("max_file_bytes", 400_000))

    for path in iter_files(cfg, subdir=subpath):
        if is_probably_binary(path):
            continue
        language = LANGUAGE_MAP.get(path.suffix.lower())
        if language in (None, "JSON", "Markdown", "YAML"):
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        issues.extend(scan_text(cfg, relative_path(cfg, path), text, language))
        if len(issues) >= max_issues:
            break

    issues.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 3), i.path, i.line))
    return issues[:max_issues]


def summarize_issues(issues: List[Issue]) -> Dict[str, int]:
    counts = {"high": 0, "medium": 0, "low": 0}
    for issue in issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1
    return counts


def available_linters() -> List[str]:
    return [name for name in ("ruff", "flake8", "mypy", "eslint", "tsc") if shutil.which(name)]
