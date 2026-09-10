"""المرور على ملفات المشروع وتحليل بنيته."""
from __future__ import annotations

import fnmatch
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .config import LANGUAGE_MAP, Config
from .safety import is_sensitive, relative_path

BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".pdf", ".zip",
    ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib", ".class",
    ".jar", ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".mov", ".wasm",
    ".pyc", ".pyo", ".bin", ".o", ".a",
}

MANIFESTS = {
    "requirements.txt": "Python", "pyproject.toml": "Python", "setup.py": "Python",
    "Pipfile": "Python", "package.json": "JavaScript/TypeScript",
    "tsconfig.json": "TypeScript", "go.mod": "Go", "Cargo.toml": "Rust",
    "pom.xml": "Java (Maven)", "build.gradle": "Java (Gradle)",
    "composer.json": "PHP", "Gemfile": "Ruby", "Dockerfile": "Docker",
    "docker-compose.yml": "Docker", "Makefile": "Make",
}


def iter_files(
    cfg: Config,
    subdir: Optional[Path] = None,
    include: Optional[List[str]] = None,
    skip_sensitive: bool = True,
) -> Iterator[Path]:
    """يمرّ على ملفات المشروع متجاهلاً المجلدات المستثناة."""
    ignore = set(cfg.ignore_names())
    base = (subdir or cfg.root).resolve()
    if not base.exists():
        return

    for path in sorted(base.rglob("*")):
        try:
            rel_parts = path.relative_to(cfg.root.resolve()).parts
        except ValueError:
            continue
        if any(part in ignore for part in rel_parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if skip_sensitive and is_sensitive(cfg, path):
            continue
        if include:
            rel = relative_path(cfg, path)
            if not any(
                fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(path.name, pat)
                for pat in include
            ):
                continue
        yield path


def is_probably_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTS:
        return True
    try:
        with path.open("rb") as fh:
            chunk = fh.read(2048)
        return b"\x00" in chunk
    except OSError:
        return True


def read_text(path: Path, max_bytes: int = 400_000) -> str:
    size = path.stat().st_size
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        text = fh.read(max_bytes)
    if size > max_bytes:
        text += f"\n... [تم اقتطاع الملف: {size} بايت إجمالاً]"
    return text


@dataclass
class ProjectAnalysis:
    root: str
    file_count: int = 0
    total_lines: int = 0
    languages: Dict[str, int] = field(default_factory=dict)
    language_lines: Dict[str, int] = field(default_factory=dict)
    manifests: List[str] = field(default_factory=list)
    entry_points: List[str] = field(default_factory=list)
    test_dirs: List[str] = field(default_factory=list)
    largest_files: List[tuple] = field(default_factory=list)
    tree: str = ""

    def to_text(self) -> str:
        lines = [f"جذر المشروع: {self.root}", f"عدد الملفات: {self.file_count}", f"إجمالي الأسطر: {self.total_lines}"]
        if self.languages:
            langs = ", ".join(
                f"{k} ({v} ملف / {self.language_lines.get(k, 0)} سطر)"
                for k, v in sorted(self.languages.items(), key=lambda x: -x[1])[:10]
            )
            lines.append(f"اللغات: {langs}")
        if self.manifests:
            lines.append("ملفات الإعداد: " + ", ".join(self.manifests))
        if self.entry_points:
            lines.append("نقاط الدخول المحتملة: " + ", ".join(self.entry_points))
        if self.test_dirs:
            lines.append("مجلدات الاختبارات: " + ", ".join(self.test_dirs))
        if self.largest_files:
            biggest = ", ".join(f"{p} ({n} سطر)" for p, n in self.largest_files[:5])
            lines.append("أكبر الملفات: " + biggest)
        if self.tree:
            lines.append("\nالبنية:\n" + self.tree)
        return "\n".join(lines)


ENTRY_CANDIDATES = {
    "main.py", "app.py", "manage.py", "__main__.py", "wsgi.py", "asgi.py",
    "index.js", "index.ts", "server.js", "server.ts", "main.go", "main.rs",
    "Program.cs", "index.php", "index.html", "main.js", "main.ts",
}


def build_tree(cfg: Config, max_entries: int = 220, max_depth: int = 4) -> str:
    ignore = set(cfg.ignore_names())
    root = cfg.root.resolve()
    lines: List[str] = [root.name + "/"]
    count = 0

    def walk(directory: Path, prefix: str, depth: int) -> None:
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return
        try:
            entries = sorted(
                [e for e in directory.iterdir() if e.name not in ignore and not e.name.startswith(".git")],
                key=lambda e: (e.is_file(), e.name.lower()),
            )
        except OSError:
            return
        for i, entry in enumerate(entries):
            if count >= max_entries:
                lines.append(prefix + "└── ...")
                return
            last = i == len(entries) - 1
            connector = "└── " if last else "├── "
            lines.append(prefix + connector + entry.name + ("/" if entry.is_dir() else ""))
            count += 1
            if entry.is_dir() and not entry.is_symlink():
                walk(entry, prefix + ("    " if last else "│   "), depth + 1)

    walk(root, "", 1)
    return "\n".join(lines)


def analyze_project(cfg: Config) -> ProjectAnalysis:
    analysis = ProjectAnalysis(root=str(cfg.root))
    lang_counter: Counter = Counter()
    lang_lines: Counter = Counter()
    sizes: List[tuple] = []
    test_dirs = set()

    for path in iter_files(cfg):
        analysis.file_count += 1
        rel = relative_path(cfg, path)
        name = path.name

        if name in MANIFESTS and "/" not in rel:
            analysis.manifests.append(name)
        if name in ENTRY_CANDIDATES:
            analysis.entry_points.append(rel)
        parts = Path(rel).parts
        for part in parts[:-1]:
            if part.lower() in {"test", "tests", "__tests__", "spec"}:
                test_dirs.add(part)

        lang = LANGUAGE_MAP.get(path.suffix.lower())
        if not lang or is_probably_binary(path):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                n_lines = sum(1 for _ in fh)
        except OSError:
            continue
        lang_counter[lang] += 1
        lang_lines[lang] += n_lines
        analysis.total_lines += n_lines
        sizes.append((rel, n_lines))

    analysis.languages = dict(lang_counter)
    analysis.language_lines = dict(lang_lines)
    analysis.test_dirs = sorted(test_dirs)
    analysis.largest_files = sorted(sizes, key=lambda x: -x[1])[:10]
    analysis.tree = build_tree(cfg)
    return analysis
