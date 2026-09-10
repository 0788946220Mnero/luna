"""توليد وعرض الفروقات (diff) قبل تعديل أي ملف."""
from __future__ import annotations

import difflib
from typing import Tuple


def make_diff(old: str, new: str, path: str, context: int = 3, max_lines: int = 400) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=context,
        )
    )
    if not diff:
        return "(لا تغييرات)"
    if len(diff) > max_lines:
        head = diff[:max_lines]
        return "".join(head) + f"\n... [تم اقتطاع {len(diff) - max_lines} سطر من الفرق]\n"
    return "".join(diff)


def diff_stats(old: str, new: str) -> Tuple[int, int]:
    """يعيد (عدد الأسطر المضافة، عدد الأسطر المحذوفة)."""
    added = removed = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed
