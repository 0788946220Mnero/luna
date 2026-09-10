"""استخراج الأرشيفات بأمان: zip-slip، zip-bomb، الروابط الرمزية، والمسارات المطلقة."""
from __future__ import annotations

import shutil
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

CHUNK = 64 * 1024


class ArchiveError(Exception):
    """أرشيف مرفوض أو تالف."""


@dataclass
class ExtractLimits:
    max_files: int = 800
    max_total_bytes: int = 200 * 1024 * 1024
    max_single_file_bytes: int = 40 * 1024 * 1024
    max_depth: int = 12


@dataclass
class ExtractResult:
    root: Path
    files: List[str] = field(default_factory=list)
    total_bytes: int = 0
    skipped: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.files)


def _reject_name(name: str, limits: ExtractLimits) -> Optional[str]:
    """يفحص اسم العضو قبل لمس القرص. يعيد سبب الرفض أو None."""
    if not name or name in (".", "..") or name.endswith("/"):
        return None if name.endswith("/") else "اسم فارغ"
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        return "مسار مطلق"
    parts = [p for p in normalized.split("/") if p]
    if any(p == ".." for p in parts):
        return "يحتوي على .."
    if len(parts) > limits.max_depth:
        return f"عمق يتجاوز {limits.max_depth}"
    if any(len(p) > 200 for p in parts):
        return "اسم مكوّن طويل جداً"
    return None


def _safe_target(dest: Path, name: str) -> Path:
    """يحسب المسار النهائي ويتأكد أنه داخل dest — بـ is_relative_to لا startswith."""
    base = dest.resolve()
    target = (base / name.replace("\\", "/")).resolve()
    if not target.is_relative_to(base):
        raise ArchiveError(f"مسار خطر داخل الأرشيف: {name}")
    return target


def detect_kind(path: Path) -> str:
    """يحدد النوع من محتوى الملف لا من امتداده."""
    try:
        if zipfile.is_zipfile(path):
            return "zip"
        if tarfile.is_tarfile(path):
            return "tar"
    except (OSError, tarfile.TarError):
        pass
    raise ArchiveError("الملف ليس أرشيف ZIP ولا TAR صالحاً (فشل فحص المحتوى).")


def extract_archive(
    archive: Path,
    dest: Path,
    limits: Optional[ExtractLimits] = None,
) -> ExtractResult:
    """يستخرج الأرشيف إلى dest.

    الحد على البايتات يُحسب أثناء الكتابة الفعلية لا من ترويسة الأرشيف،
    لأن الحجم المصرّح في الترويسة يكتبه من صنع الملف ويمكن الكذب فيه.
    """
    limits = limits or ExtractLimits()
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    result = ExtractResult(root=dest)
    kind = detect_kind(archive)

    try:
        if kind == "zip":
            _extract_zip(archive, dest, limits, result)
        else:
            _extract_tar(archive, dest, limits, result)
    except ArchiveError:
        raise
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        raise ArchiveError(f"الأرشيف تالف: {exc}") from exc
    except OSError as exc:
        raise ArchiveError(f"فشل الاستخراج: {exc}") from exc

    if not result.files:
        raise ArchiveError("الأرشيف فارغ أو كل محتوياته مرفوضة.")
    return result


def _account(result: ExtractResult, limits: ExtractLimits, written: int) -> None:
    result.total_bytes += written
    if result.total_bytes > limits.max_total_bytes:
        raise ArchiveError(
            f"المحتوى المستخرج تجاوز الحد ({limits.max_total_bytes // (1024*1024)}MB) — أرشيف مشبوه."
        )


def _stream_out(src, target: Path, limits: ExtractLimits, result: ExtractResult) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("wb") as out:
        while True:
            chunk = src.read(CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > limits.max_single_file_bytes:
                out.close()
                target.unlink(missing_ok=True)
                raise ArchiveError(f"ملف داخل الأرشيف تجاوز الحد الفردي: {target.name}")
            if result.total_bytes + written > limits.max_total_bytes:
                out.close()
                target.unlink(missing_ok=True)
                raise ArchiveError("المحتوى المستخرج تجاوز الحد الكلي — أرشيف مشبوه.")
            out.write(chunk)
    return written


def _extract_zip(archive: Path, dest: Path, limits: ExtractLimits, result: ExtractResult) -> None:
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if len(members) > limits.max_files:
            raise ArchiveError(f"عدد الملفات ({len(members)}) يتجاوز الحد ({limits.max_files}).")

        for member in members:
            name = member.filename
            if member.is_dir():
                continue

            # الرابط الرمزي في ZIP يُشفَّر في أعلى 16 بت من external_attr
            mode = member.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:
                result.skipped.append(f"{name} (رابط رمزي)")
                continue

            reason = _reject_name(name, limits)
            if reason:
                result.skipped.append(f"{name} ({reason})")
                continue

            target = _safe_target(dest, name)
            with zf.open(member) as src:
                written = _stream_out(src, target, limits, result)
            _account(result, limits, written)
            result.files.append(str(target.relative_to(dest).as_posix()))


def _extract_tar(archive: Path, dest: Path, limits: ExtractLimits, result: ExtractResult) -> None:
    with tarfile.open(archive) as tf:
        members = tf.getmembers()
        if len(members) > limits.max_files:
            raise ArchiveError(f"عدد الملفات ({len(members)}) يتجاوز الحد ({limits.max_files}).")

        for member in members:
            if member.issym() or member.islnk():
                result.skipped.append(f"{member.name} (رابط)")
                continue
            if member.isdev() or member.isfifo():
                result.skipped.append(f"{member.name} (ملف جهاز)")
                continue
            if not member.isfile():
                continue

            reason = _reject_name(member.name, limits)
            if reason:
                result.skipped.append(f"{member.name} ({reason})")
                continue

            target = _safe_target(dest, member.name)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src:
                written = _stream_out(src, target, limits, result)
            _account(result, limits, written)
            result.files.append(str(target.relative_to(dest).as_posix()))


def make_zip(source_dir: Path, output: Path, max_files: int = 2000) -> Path:
    """يحزم مجلداً في ZIP لتنزيله."""
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise ArchiveError("المجلد المطلوب حزمه غير موجود.")
    output.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            count += 1
            if count > max_files:
                break
            zf.write(path, path.relative_to(source_dir).as_posix())
    if count == 0:
        output.unlink(missing_ok=True)
        raise ArchiveError("لا توجد ملفات لحزمها.")
    return output


def purge_old(base: Path, max_age_seconds: int) -> int:
    """يحذف مجلدات الجلسات الأقدم من العمر المحدد. يعيد عدد المحذوف."""
    import time

    if not base.is_dir():
        return 0
    now = time.time()
    removed = 0
    for child in base.iterdir():
        if not child.is_dir():
            continue
        try:
            if now - child.stat().st_mtime > max_age_seconds:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
