"""طبقة الأمان: حراسة المسارات، الملفات الحساسة، وفحص أوامر الطرفية."""
from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .config import Config


class SafetyError(Exception):
    """يُرفع عند محاولة عملية تخالف قواعد الأمان."""


# ---------------------------------------------------------------- المسارات

def resolve_in_root(cfg: Config, path: str | Path) -> Path:
    """يحوّل المسار إلى مسار مطلق ويتأكد أنه داخل جذر المشروع."""
    if path is None or str(path).strip() == "":
        raise SafetyError("المسار فارغ.")

    raw = Path(str(path).strip())
    root = cfg.root.resolve()
    candidate = raw if raw.is_absolute() else (root / raw)

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:  # روابط دائرية مثلاً
        raise SafetyError(f"تعذّر تحليل المسار '{path}': {exc}") from exc

    if cfg.safety.get("allow_outside_root", False):
        return resolved

    if resolved != root and root not in resolved.parents:
        raise SafetyError(
            f"المسار '{path}' يقع خارج جذر المشروع ({root}). العملية مرفوضة."
        )
    return resolved


def relative_path(cfg: Config, path: Path) -> str:
    try:
        return path.resolve().relative_to(cfg.root.resolve()).as_posix()
    except ValueError:
        return str(path)


def is_ignored(cfg: Config, path: Path) -> bool:
    """هل المسار داخل مجلد مُستثنى؟"""
    ignore = set(cfg.ignore_names())
    try:
        rel = path.resolve().relative_to(cfg.root.resolve())
    except ValueError:
        return False
    return any(part in ignore for part in rel.parts)


def is_sensitive(cfg: Config, path: Path) -> bool:
    """هل الملف حساس (مفاتيح، كلمات مرور، .env ...)؟"""
    patterns: List[str] = cfg.safety.get("sensitive_patterns", [])
    rel = relative_path(cfg, path)
    name = Path(rel).name
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
            return True
        if "/" in pattern and fnmatch.fnmatch(rel, f"*/{pattern}"):
            return True
    return False


# ---------------------------------------------------------------- الأوامر

_SPLIT_RE = re.compile(r"&&|\|\||;|\|")


@dataclass
class CommandVerdict:
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    binaries: List[str] = field(default_factory=list)
    unlisted: List[str] = field(default_factory=list)

    @property
    def needs_extra_confirmation(self) -> bool:
        return bool(self.unlisted)


def analyze_command(cfg: Config, command: str) -> CommandVerdict:
    """يفحص أمر الطرفية مقابل قائمة الحظر وقائمة السماح."""
    verdict = CommandVerdict(allowed=True)
    text = (command or "").strip()
    if not text:
        return CommandVerdict(allowed=False, reasons=["الأمر فارغ."])

    for pattern in cfg.safety.get("denied_command_patterns", []):
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                verdict.allowed = False
                verdict.reasons.append(f"يطابق نمطاً محظوراً: {pattern}")
        except re.error:
            continue

    if re.search(r"(^|\s)>\s*/(?!tmp)", text):
        verdict.allowed = False
        verdict.reasons.append("إعادة توجيه الإخراج إلى مسار نظام مطلق.")

    allowed_bins = set(cfg.safety.get("allowed_commands", []))
    for segment in _SPLIT_RE.split(text):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError as exc:
            verdict.allowed = False
            verdict.reasons.append(f"تعذّر تحليل الأمر: {exc}")
            continue
        if not tokens:
            continue
        binary = Path(tokens[0]).name
        # تجاهل تعيينات المتغيرات في البداية (VAR=value cmd ...)
        idx = 0
        while idx < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[idx]):
            idx += 1
        if idx < len(tokens):
            binary = Path(tokens[idx]).name
        verdict.binaries.append(binary)
        if binary not in allowed_bins:
            verdict.unlisted.append(binary)

    if verdict.unlisted:
        verdict.reasons.append(
            "أوامر غير موجودة في قائمة السماح: " + ", ".join(sorted(set(verdict.unlisted)))
        )
    return verdict


def assert_command_allowed(cfg: Config, command: str) -> CommandVerdict:
    verdict = analyze_command(cfg, command)
    if not verdict.allowed:
        raise SafetyError("أمر خطير مرفوض: " + " | ".join(verdict.reasons))
    return verdict
