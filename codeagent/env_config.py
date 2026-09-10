"""إعدادات الخصوصية والتشغيل من متغيرات البيئة (.env).

الأولوية: متغير البيئة > agent.yaml > الافتراضي.
لا تُطبع أي قيمة سرية في السجلات أو الاستجابات.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PREFIX = "CODEAGENT_"
SECRET_KEYS = {"CODEAGENT_API_KEY"}


def load_dotenv(path: Path | str = ".env", override: bool = False) -> Dict[str, str]:
    """محمّل .env بسيط بلا تبعيات خارجية. لا يفشل إن كان الملف غائباً."""
    target = Path(path)
    loaded: Dict[str, str] = {}
    if not target.is_file():
        return loaded

    for raw in target.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "نعم"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _list(name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class ServerSettings:
    """كل ما يتحكم بسلوك الخادم والخصوصية."""

    # الشبكة
    host: str = "127.0.0.1"
    port: int = 8787
    allowed_origins: List[str] = field(default_factory=list)

    # المصادقة
    api_key: Optional[str] = None
    require_auth: bool = True

    # المشروع
    project_root: Path = field(default_factory=lambda: Path.cwd())

    # النماذج
    llm_provider: str = "openai"           # openai | ollama | anthropic
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    ollama_host: str = "http://localhost:11434"
    text_model: str = "gpt-5.6-sol"
    vision_model: str = "gpt-5.6-sol"
    request_timeout: int = 300

    # الخصوصية والصور
    store_uploads: bool = False          # false = تُحذف الصورة فور المعالجة
    upload_dir: Optional[Path] = None
    max_upload_mb: int = 12
    strip_exif: bool = True              # تجريد بيانات EXIF (موقع، جهاز، وقت)
    max_image_dimension: int = 1600      # تصغير الصور الكبيرة قبل الإرسال
    redact_logs: bool = True             # عدم كتابة محتوى الصور/النصوص في السجل

    # الأرشيفات
    max_archive_mb: int = 50
    max_extracted_mb: int = 200
    max_archive_files: int = 800
    session_ttl_hours: int = 6

    # الذاكرة
    mongodb_uri: str = ""
    mongodb_db: str = "codeagent"
    memory_keep_recent: int = 16      # رسائل تُرسل حرفياً
    memory_summary_trigger: int = 10  # رسائل زائدة قبل التلخيص
    memory_facts_every: int = 12      # كل كم رسالة نستخرج حقائق
    memory_enabled: bool = True

    # الحسابات
    admin_user: str = ""
    admin_password: str = ""
    session_days: int = 7
    isolate_users: bool = True

    # الأمان
    allow_apply: bool = True             # السماح بكتابة الملفات من الواجهة
    auto_approve: bool = False           # يبقى false: التطبيق يتم بضغطة صريحة
    rate_limit_per_minute: int = 30

    def public_dict(self) -> Dict[str, Any]:
        """نسخة آمنة للعرض في الواجهة — بلا أي أسرار."""
        return {
            "project_root": str(self.project_root),
            "llm_provider": self.llm_provider,
            "cloud_provider": self.llm_provider in ("openai", "api", "groq", "openrouter"),
            "max_archive_mb": self.max_archive_mb,
            "text_model": self.text_model,
            "vision_model": self.vision_model,
            "ollama_host": self.ollama_host,
            "store_uploads": self.store_uploads,
            "strip_exif": self.strip_exif,
            "max_upload_mb": self.max_upload_mb,
            "allow_apply": self.allow_apply,
            "require_auth": self.require_auth,
            "isolate_users": self.isolate_users,
            "memory_enabled": self.memory_enabled,
            "memory_backend": "mongodb" if self.mongodb_uri else "sqlite",
            "rate_limit_per_minute": self.rate_limit_per_minute,
        }


def load_settings(env_file: Path | str = ".env") -> ServerSettings:
    load_dotenv(env_file)

    root_raw = os.environ.get(f"{PREFIX}PROJECT_ROOT") or os.getcwd()
    root = Path(root_raw).expanduser().resolve()

    upload_raw = os.environ.get(f"{PREFIX}UPLOAD_DIR")
    upload_dir = Path(upload_raw).expanduser().resolve() if upload_raw else None

    provider = os.environ.get(f"{PREFIX}LLM_PROVIDER", "openai").strip().lower()
    # افتراضيات النماذج تتبع المزوّد حتى لا يُرسل اسم نموذج محلي إلى مزوّد سحابي
    if provider in ("anthropic", "claude"):
        default_text = default_vision = "claude-sonnet-5"
        default_base = "https://api.anthropic.com"
    elif provider in ("openai", "api", "groq", "openrouter"):
        default_text = default_vision = "gpt-5.6-sol"
        default_base = "https://api.openai.com/v1"
    else:
        default_text, default_vision = "qwen2.5-coder:7b", "qwen2.5vl:7b"
        default_base = "https://api.openai.com/v1"

    settings = ServerSettings(
        host=os.environ.get(f"{PREFIX}HOST", "127.0.0.1"),
        port=_int("PORT", _int(f"{PREFIX}PORT", 8787)),
        allowed_origins=_list(f"{PREFIX}ALLOWED_ORIGINS", []),
        api_key=os.environ.get(f"{PREFIX}API_KEY") or None,
        require_auth=_bool(f"{PREFIX}REQUIRE_AUTH", True),
        project_root=root,
        llm_provider=provider,
        llm_api_key=os.environ.get(f"{PREFIX}LLM_API_KEY", ""),
        llm_base_url=os.environ.get(f"{PREFIX}LLM_BASE_URL", default_base),
        ollama_host=os.environ.get(f"{PREFIX}OLLAMA_HOST", "http://localhost:11434"),
        text_model=os.environ.get(f"{PREFIX}TEXT_MODEL", default_text),
        vision_model=os.environ.get(f"{PREFIX}VISION_MODEL", default_vision),
        request_timeout=_int(f"{PREFIX}REQUEST_TIMEOUT", 300),
        store_uploads=_bool(f"{PREFIX}STORE_UPLOADS", False),
        upload_dir=upload_dir,
        max_upload_mb=_int(f"{PREFIX}MAX_UPLOAD_MB", 12),
        strip_exif=_bool(f"{PREFIX}STRIP_EXIF", True),
        max_image_dimension=_int(f"{PREFIX}MAX_IMAGE_DIMENSION", 1600),
        redact_logs=_bool(f"{PREFIX}REDACT_LOGS", True),
        max_archive_mb=_int(f"{PREFIX}MAX_ARCHIVE_MB", 50),
        max_extracted_mb=_int(f"{PREFIX}MAX_EXTRACTED_MB", 200),
        max_archive_files=_int(f"{PREFIX}MAX_ARCHIVE_FILES", 800),
        session_ttl_hours=_int(f"{PREFIX}SESSION_TTL_HOURS", 6),
        mongodb_uri=os.environ.get(f"{PREFIX}MONGODB_URI", "") or os.environ.get("MONGODB_URI", ""),
        mongodb_db=os.environ.get(f"{PREFIX}MONGODB_DB", "codeagent"),
        memory_keep_recent=_int(f"{PREFIX}MEMORY_KEEP_RECENT", 16),
        memory_summary_trigger=_int(f"{PREFIX}MEMORY_SUMMARY_TRIGGER", 10),
        memory_facts_every=_int(f"{PREFIX}MEMORY_FACTS_EVERY", 12),
        memory_enabled=_bool(f"{PREFIX}MEMORY_ENABLED", True),
        admin_user=os.environ.get(f"{PREFIX}ADMIN_USER", ""),
        admin_password=os.environ.get(f"{PREFIX}ADMIN_PASSWORD", ""),
        session_days=_int(f"{PREFIX}SESSION_DAYS", 7),
        isolate_users=_bool(f"{PREFIX}ISOLATE_USERS", True),
        allow_apply=_bool(f"{PREFIX}ALLOW_APPLY", True),
        auto_approve=_bool(f"{PREFIX}AUTO_APPROVE", False),
        rate_limit_per_minute=_int(f"{PREFIX}RATE_LIMIT_PER_MINUTE", 30),
    )
    return settings


def apply_to_config(settings: ServerSettings, cfg) -> None:
    """يدمج إعدادات البيئة داخل كائن Config الخاص بالوكيل."""
    cfg.llm["provider"] = settings.llm_provider
    cfg.llm["api_key"] = settings.llm_api_key
    cfg.llm["base_url"] = settings.llm_base_url
    cfg.llm["host"] = settings.ollama_host
    cfg.llm["model"] = settings.text_model
    cfg.llm["timeout"] = settings.request_timeout
    cfg.data.setdefault("vision", {})
    cfg.data["vision"].update(
        {
            "model": settings.vision_model,
            "max_dimension": settings.max_image_dimension,
            "strip_exif": settings.strip_exif,
        }
    )
    cfg.safety["auto_approve"] = settings.auto_approve
