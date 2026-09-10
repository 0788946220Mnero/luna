"""اكتشاف إطار الاختبارات وتشغيله وتحليل النتائج."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..approvals import ApprovalRequest
from ..config import Config
from ..safety import SafetyError
from .base import Tool, ToolContext, ToolResult, obj, string
from .shell import execute


def detect_test_command(cfg: Config) -> Optional[Tuple[str, str]]:
    """يعيد (الأمر، اسم الإطار) حسب ملفات المشروع."""
    root = cfg.root

    if (root / "pytest.ini").exists() or (root / "tests").is_dir() or list(root.glob("test_*.py")):
        return "python -m pytest -q", "pytest"
    if (root / "pyproject.toml").exists():
        try:
            content = (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
            if "pytest" in content:
                return "python -m pytest -q", "pytest"
        except OSError:
            pass

    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            scripts = data.get("scripts", {}) or {}
            if "test" in scripts:
                return "npm test --silent", "npm"
        except (json.JSONDecodeError, OSError):
            pass

    if (root / "go.mod").exists():
        return "go test ./...", "go"
    if (root / "Cargo.toml").exists():
        return "cargo test", "cargo"
    if (root / "pom.xml").exists():
        return "mvn -q test", "maven"
    if list(root.glob("*.csproj")) or list(root.glob("**/*.csproj")):
        return "dotnet test", "dotnet"
    if (root / "composer.json").exists() and (root / "vendor" / "bin" / "phpunit").exists():
        return "vendor/bin/phpunit", "phpunit"
    return None


SUMMARY_PATTERNS = [
    re.compile(r"(\d+) failed[,\s]+(\d+) passed", re.IGNORECASE),
    re.compile(r"(\d+) passed", re.IGNORECASE),
    re.compile(r"Tests:\s+(\d+) failed,\s+(\d+) passed", re.IGNORECASE),
    re.compile(r"ok\s+\S+", re.IGNORECASE),
]


def summarize(output: str) -> str:
    lines = [ln for ln in output.splitlines() if ln.strip()]
    tail = lines[-15:] if len(lines) > 15 else lines
    for pattern in SUMMARY_PATTERNS:
        match = pattern.search(output)
        if match:
            return f"ملخّص: {match.group(0)}"
    return "آخر أسطر المخرجات:\n" + "\n".join(tail)


def extract_failures(output: str, limit: int = 12) -> List[str]:
    failures = []
    for line in output.splitlines():
        stripped = line.strip()
        if re.match(r"^(FAILED|ERROR|E\s+|--- FAIL|test .* FAILED|✕|×)", stripped):
            failures.append(stripped[:300])
        elif "AssertionError" in stripped or "Traceback (most recent call last)" in stripped:
            failures.append(stripped[:300])
        if len(failures) >= limit:
            break
    return failures


def _run_tests(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    explicit = (args.get("command") or "").strip()
    if explicit:
        command, framework = explicit, "custom"
    else:
        detected = detect_test_command(ctx.cfg)
        if not detected:
            return ToolResult.failure(
                "لم يُعثر على إطار اختبارات معروف. مرّر command صراحةً، مثل: python -m pytest -q"
            )
        command, framework = detected

    target = (args.get("target") or "").strip()
    if target:
        command = f"{command} {target}"

    ctx.approvals.require(
        ApprovalRequest(
            tool="run_tests",
            summary=f"تشغيل الاختبارات ({framework}): {command}",
            details=f"المجلد: {ctx.cfg.root}",
            risk="medium",
            targets=[command],
        )
    )

    try:
        out = execute(ctx.cfg, command, timeout=args.get("timeout"))
    except SafetyError as exc:
        return ToolResult.failure(str(exc))

    combined = (out.stdout or "") + "\n" + (out.stderr or "")
    failures = extract_failures(combined)
    body = [
        f"الأمر: {command}",
        f"الإطار: {framework}",
        f"رمز الخروج: {out.returncode}",
        summarize(combined),
    ]
    if failures:
        body.append("الإخفاقات المكتشفة:\n" + "\n".join(f"- {f}" for f in failures))
    body.append("--- المخرجات ---\n" + combined.strip()[:12000])

    ctx.emit(f"اختبارات {framework}: رمز {out.returncode}")
    return ToolResult(
        ok=out.returncode == 0,
        output="\n\n".join(body),
        data={"returncode": out.returncode, "framework": framework, "failures": failures},
    )


TOOLS = [
    Tool(
        name="run_tests",
        description=(
            "اكتشف إطار الاختبارات في المشروع (pytest / npm / go / cargo / dotnet / maven / phpunit) "
            "وشغّله، ثم أعد ملخّصاً بالإخفاقات."
        ),
        parameters=obj(
            {
                "command": string("أمر اختبار مخصّص (اختياري)"),
                "target": string("ملف أو اختبار محدد (اختياري)"),
            }
        ),
        handler=_run_tests,
        mutating=True,
        risk="medium",
    )
]
