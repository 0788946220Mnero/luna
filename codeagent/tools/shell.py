"""تنفيذ أوامر الطرفية بشكل آمن (allowlist + موافقة + sandbox اختياري)."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..approvals import ApprovalRequest
from ..config import Config
from ..safety import SafetyError, analyze_command, assert_command_allowed
from .base import Tool, ToolContext, ToolResult, integer, obj, string

MAX_OUTPUT = 20_000


@dataclass
class CommandOutput:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    sandbox: str = "none"

    def render(self) -> str:
        parts = [f"رمز الخروج: {self.returncode}" + (" (انتهت المهلة)" if self.timed_out else "")]
        if self.stdout.strip():
            parts.append("--- stdout ---\n" + self.stdout.strip()[:MAX_OUTPUT])
        if self.stderr.strip():
            parts.append("--- stderr ---\n" + self.stderr.strip()[:MAX_OUTPUT])
        if not self.stdout.strip() and not self.stderr.strip():
            parts.append("(لا مخرجات)")
        return "\n".join(parts)


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=15, check=False
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def build_docker_argv(cfg: Config, command: str) -> List[str]:
    sb = cfg.sandbox
    return [
        "docker", "run", "--rm",
        "--network", str(sb.get("network", "none")),
        "--memory", str(sb.get("memory", "512m")),
        "--pids-limit", "256",
        "-v", f"{cfg.root}:/workspace",
        "-w", "/workspace",
        str(sb.get("image", "python:3.11-slim")),
        "sh", "-lc", command,
    ]


def execute(cfg: Config, command: str, timeout: Optional[int] = None, cwd: Optional[str] = None) -> CommandOutput:
    """ينفّذ الأمر بعد اجتياز فحص الأمان. يفترض أن الموافقة أُخذت مسبقاً."""
    assert_command_allowed(cfg, command)
    timeout = int(timeout or cfg.sandbox.get("timeout", 120))
    mode = str(cfg.sandbox.get("mode", "none")).lower()

    env = {
        k: v for k, v in os.environ.items()
        if not any(s in k.upper() for s in ("SECRET", "TOKEN", "PASSWORD", "API_KEY", "CREDENTIAL"))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    workdir = str(cwd or cfg.root)

    if mode == "docker":
        if not docker_available():
            raise SafetyError("وضع sandbox = docker لكن Docker غير متاح. شغّل Docker أو غيّر sandbox.mode إلى none.")
        argv = build_docker_argv(cfg, command)
        shell = False
    else:
        argv = command
        shell = True

    try:
        proc = subprocess.run(
            argv,
            shell=shell,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        return CommandOutput(proc.returncode, proc.stdout, proc.stderr, sandbox=mode)
    except subprocess.TimeoutExpired as exc:
        return CommandOutput(
            124,
            exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            f"انتهت مهلة التنفيذ بعد {timeout} ثانية.",
            timed_out=True,
            sandbox=mode,
        )
    except OSError as exc:
        return CommandOutput(127, "", f"تعذّر تنفيذ الأمر: {exc}", sandbox=mode)


def _run_command(ctx: ToolContext, args: Dict[str, Any]) -> ToolResult:
    command = (args.get("command") or "").strip()
    if not command:
        return ToolResult.failure("لم يُحدَّد أمر.")

    verdict = analyze_command(ctx.cfg, command)
    if not verdict.allowed:
        return ToolResult.failure("أمر مرفوض لأسباب أمنية: " + " | ".join(verdict.reasons))

    mode = str(ctx.cfg.sandbox.get("mode", "none")).lower()
    details = f"الأمر: {command}\nالمجلد: {ctx.cfg.root}\nالعزل: {mode}"
    if verdict.unlisted:
        details += "\nتحذير: " + " | ".join(verdict.reasons)

    ctx.approvals.require(
        ApprovalRequest(
            tool="run_command",
            summary=f"تنفيذ أمر طرفية: {command[:120]}",
            details=details,
            risk="high" if verdict.unlisted else "medium",
            targets=[command],
        ),
        force=bool(verdict.unlisted),
    )

    try:
        out = execute(ctx.cfg, command, timeout=args.get("timeout"))
    except SafetyError as exc:
        return ToolResult.failure(str(exc))

    ctx.emit(f"نفّذ: {command} (رمز {out.returncode})")
    return ToolResult(
        ok=out.returncode == 0,
        output=out.render(),
        data={"returncode": out.returncode, "command": command},
    )


TOOLS = [
    Tool(
        name="run_command",
        description=(
            "نفّذ أمر طرفية داخل مجلد المشروع. الأوامر الخطيرة محظورة، والأوامر خارج قائمة السماح "
            "تتطلب تأكيداً إضافياً. استخدمه للبناء والتثبيت وفحص الأدوات."
        ),
        parameters=obj(
            {
                "command": string("الأمر المراد تنفيذه"),
                "timeout": integer("مهلة التنفيذ بالثواني (اختياري)"),
            },
            required=["command"],
        ),
        handler=_run_command,
        mutating=True,
        risk="high",
    )
]
