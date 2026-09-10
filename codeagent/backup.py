"""النسخ الاحتياطي والتراجع (Undo) عن تعديلات الملفات."""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .config import Config
from .safety import relative_path
from .store import Store

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class RestoreResult:
    op_id: int
    tool: str
    restored: List[str]
    deleted: List[str]
    errors: List[str]


class BackupManager:
    """يأخذ لقطة من الملف قبل أي تعديل، ويستطيع استعادتها لاحقاً."""

    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    def _slug(self, rel: str) -> str:
        return _SLUG_RE.sub("_", rel).strip("_")[:120] or "file"

    def snapshot(self, op_id: int, path: Path) -> Optional[Path]:
        """ينسخ الملف إلى مجلد النسخ الاحتياطية ويسجّله. يعيد مسار النسخة."""
        self.cfg.ensure_state_dir()
        rel = relative_path(self.cfg, path)
        existed = path.exists() and path.is_file()

        if not existed:
            self.store.add_backup(op_id, rel, None, existed=False)
            return None

        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        target = self.cfg.backups_dir / f"{stamp}__{self._slug(rel)}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        self.store.add_backup(op_id, rel, str(target), existed=True)
        return target

    def restore_operation(self, op_id: int) -> RestoreResult:
        """يعيد جميع الملفات المرتبطة بعملية إلى حالتها قبل التنفيذ."""
        op_rows = self.store.recent_operations(limit=10_000)
        op = next((o for o in op_rows if o.id == op_id), None)
        result = RestoreResult(op_id=op_id, tool=op.tool if op else "?", restored=[], deleted=[], errors=[])

        rows = self.store.backups_for(op_id)
        if not rows:
            result.errors.append("لا توجد نسخ احتياطية مرتبطة بهذه العملية.")
            return result

        for row in rows:
            target = self.cfg.root / row["rel_path"]
            try:
                if row["existed"] and row["backup_path"]:
                    src = Path(row["backup_path"])
                    if not src.exists():
                        result.errors.append(f"النسخة الاحتياطية مفقودة: {row['rel_path']}")
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
                    result.restored.append(row["rel_path"])
                else:
                    # الملف لم يكن موجوداً قبل العملية => نحذفه للعودة للحالة السابقة
                    if target.exists():
                        target.unlink()
                        result.deleted.append(row["rel_path"])
            except OSError as exc:
                result.errors.append(f"{row['rel_path']}: {exc}")

        if not result.errors:
            self.store.mark_undone(op_id)
        return result


# ----------------------------------------------------------------- Git

def git_available(cfg: Config) -> bool:
    if shutil.which("git") is None:
        return False
    return (cfg.root / ".git").exists()


def run_git(cfg: Config, args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cfg.root),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
