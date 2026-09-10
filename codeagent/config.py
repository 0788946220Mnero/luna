"""تحميل وإدارة إعدادات الوكيل (agent.yaml)."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml

CONFIG_FILENAME = "agent.yaml"
STATE_DIRNAME = ".codeagent"

DEFAULTS: Dict[str, Any] = {
    "llm": {
        "provider": "ollama",          # ollama | mock
        "host": "http://localhost:11434",
        "model": "qwen2.5-coder:7b",
        "temperature": 0.1,
        "num_ctx": 8192,
        "timeout": 300,
        "max_steps": 20,
    },
    "safety": {
        "allow_outside_root": False,
        "auto_approve": False,
        "read_sensitive_requires_approval": True,
        "require_approval_for": [
            "create_file", "write_file", "edit_file", "delete_file",
            "run_command", "run_tests",
        ],
        "sensitive_patterns": [
            ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
            "id_rsa", "id_rsa.*", "id_ed25519", "id_ed25519.*",
            ".ssh/*", ".aws/*", ".gnupg/*",
            "*secret*", "*credential*", "*password*",
            "*.sqlite", "*.db",
            "service-account*.json", "google-services.json",
        ],
        "allowed_commands": [
            "python", "python3", "pip", "pytest", "ruff", "flake8", "black", "mypy",
            "node", "npm", "npx", "yarn", "pnpm", "tsc", "jest", "vitest", "eslint",
            "go", "cargo", "rustc", "dotnet", "mvn", "gradle", "java", "javac",
            "php", "composer", "phpunit",
            "git", "ls", "cat", "head", "tail", "wc", "echo", "pwd", "which",
            "grep", "find", "sort", "uniq", "diff", "mkdir", "touch", "cp", "mv",
            "make", "sed", "awk", "tree", "date", "env",
        ],
        "denied_command_patterns": [
            r"\brm\s+(-[a-zA-Z]*\s+)*-?[a-zA-Z]*[rf]",
            r"\bmkfs(\.\w+)?\b",
            r"\bdd\s+if=",
            r"\b(shutdown|reboot|halt|poweroff|init\s+0)\b",
            r"\bformat\b\s+[a-zA-Z]:",
            r":\s*\(\s*\)\s*\{.*\};\s*:",
            r"\bchmod\s+(-R\s+)?777\s+/",
            r"\bchown\s+-R\s+.*\s+/\s*$",
            r"\bcurl\b[^|]*\|\s*(ba)?sh",
            r"\bwget\b[^|]*\|\s*(ba)?sh",
            r"/etc/(passwd|shadow|sudoers)",
            r"~?/?\.ssh/",
            r"\.aws/credentials",
            r"\bhistory\b\s*\|",
            r"\bgit\s+push\b.*(--force|-f)\b",
            r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f)",
            r"\bsudo\b",
            r"\bsu\s+-",
            r"\bkill(all)?\s+-9\s+1\b",
            r">\s*/dev/sd[a-z]",
            r"\bcrontab\b",
            r"\bnc\s+-l",
            r"\bopenssl\s+(enc|rsa)\b.*-in\s+.*\.(key|pem)",
        ],
    },
    "sandbox": {
        "mode": "none",                # none | docker
        "image": "python:3.11-slim",
        "network": "none",
        "timeout": 120,
        "memory": "512m",
    },
    "validation": {
        "strict": False,      # True = يرفض حفظ ملف لم يجتز التحقق
    },
    "project": {
        "max_file_bytes": 400_000,
        "max_read_lines": 1500,
        "ignore": [
            ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
            "dist", "build", ".next", ".nuxt", "target", "bin", "obj",
            ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode",
            "vendor", "coverage", ".codeagent",
        ],
    },
}

LANGUAGE_MAP = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".java": "Java", ".cs": "C#", ".go": "Go", ".rs": "Rust", ".php": "PHP",
    ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "CSS", ".sass": "CSS",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".md": "Markdown",
    ".sh": "Shell", ".sql": "SQL", ".vue": "Vue", ".rb": "Ruby", ".kt": "Kotlin",
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    root: Path
    data: Dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    source: Path | None = None

    # ---- وصول مختصر للأقسام ----
    @property
    def llm(self) -> Dict[str, Any]:
        return self.data["llm"]

    @property
    def safety(self) -> Dict[str, Any]:
        return self.data["safety"]

    @property
    def sandbox(self) -> Dict[str, Any]:
        return self.data["sandbox"]

    @property
    def project(self) -> Dict[str, Any]:
        return self.data["project"]

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIRNAME

    @property
    def db_path(self) -> Path:
        return self.state_dir / "agent.db"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "agent.log"

    def ensure_state_dir(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        gitignore = self.state_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")

    def ignore_names(self) -> List[str]:
        return list(self.project.get("ignore", []))


def find_config_file(start: Path) -> Path | None:
    """يبحث عن agent.yaml في المجلد الحالي ثم المجلدات الأعلى."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        cfg = candidate / CONFIG_FILENAME
        if cfg.is_file():
            return cfg
    return None


def load_config(start: Path | str = ".") -> Config:
    """يحمّل الإعدادات؛ إن لم يوجد agent.yaml يستخدم القيم الافتراضية."""
    start_path = Path(start).resolve()
    cfg_file = find_config_file(start_path)
    if cfg_file is None:
        return Config(root=start_path)

    try:
        raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"agent.yaml غير صالح: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("agent.yaml يجب أن يحتوي على قاموس (mapping) في الجذر.")

    merged = _deep_merge(DEFAULTS, raw)
    root = cfg_file.parent
    if raw.get("root"):
        candidate = Path(raw["root"])
        root = candidate if candidate.is_absolute() else (cfg_file.parent / candidate)
    return Config(root=root.resolve(), data=merged, source=cfg_file)


def write_default_config(root: Path, model: str | None = None) -> Path:
    """ينشئ ملف agent.yaml افتراضياً داخل جذر المشروع."""
    data = copy.deepcopy(DEFAULTS)
    if model:
        data["llm"]["model"] = model
    target = root / CONFIG_FILENAME
    header = (
        "# إعدادات الوكيل البرمجي codeagent\n"
        "# عدّل القيم حسب حاجتك. المسارات كلها نسبية لجذر المشروع.\n"
    )
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)
    target.write_text(header + body, encoding="utf-8")
    return target
