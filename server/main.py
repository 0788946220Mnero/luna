"""الباك اند: خادم FastAPI للوكيل البرمجي مع دعم الرؤية.

التشغيل:
    uvicorn server.main:app --host 127.0.0.1 --port 8787
    أو: python -m server

كل الإعدادات تأتي من متغيرات البيئة / ملف .env — راجع .env.example.
"""
from __future__ import annotations

import hmac
import json
import re
import logging
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from codeagent import __version__
from codeagent.accounts import Accounts, AuthError, User, validate_password
from codeagent.agent import Agent
from codeagent.archives import (
    ArchiveError,
    ExtractLimits,
    extract_archive,
    make_zip,
    purge_old,
)
from codeagent.approvals import ApprovalManager
from codeagent.backup import BackupManager, git_available
from codeagent.config import Config, load_config
from codeagent.diffs import diff_stats, make_diff
from codeagent.env_config import ServerSettings, apply_to_config, load_settings
from codeagent.executor import Executor
from codeagent.llm import LLMError, build_client
from codeagent.memory import (
    MemoryStore,
    build_memory,
    extract_facts,
    summarize_messages,
)
from codeagent.review import run_review, summarize_issues
from codeagent.safety import SafetyError, is_sensitive, relative_path, resolve_in_root
from codeagent.store import Store
from codeagent.tools.vision_tools import build_vision_client
from codeagent.vision import GeneratedFile, ImageError, ImageInput
from codeagent.walker import analyze_project, is_probably_binary

logger = logging.getLogger("codeagent.server")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

SETTINGS: ServerSettings = load_settings()
CFG: Config = load_config(SETTINGS.project_root)
CFG.root = SETTINGS.project_root
apply_to_config(SETTINGS, CFG)
CFG.ensure_state_dir()

@asynccontextmanager
async def lifespan(_: FastAPI):
    """فحوصات بدء التشغيل: تحذير صريح إن كان الخادم مكشوفاً أو بلا مفتاح."""
    logging.basicConfig(level=logging.INFO)
    if SETTINGS.require_auth and not SETTINGS.api_key:
        logger.error(
            "لم يُضبط CODEAGENT_API_KEY. كل الطلبات المحمية سترفض. "
            "ولّد مفتاحاً: python -c \"import secrets;print(secrets.token_urlsafe(32))\""
        )
    if SETTINGS.host not in {"127.0.0.1", "localhost", "::1"} and not SETTINGS.api_key:
        logger.error("الخادم مكشوف على الشبكة بلا مفتاح API — أوقفه واضبط CODEAGENT_API_KEY فوراً.")
    if SETTINGS.llm_provider in ("openai", "api", "groq", "openrouter"):
        if not SETTINGS.llm_api_key:
            logger.warning("لا يوجد مفتاح نموذج API حالياً؛ سيستمر الخادم بالعمل وستكون ميزات النموذج غير متاحة فقط.")
        logger.warning(
            "المزوّد الخارجي مفعّل (%s): محتوى الملفات والصور سيغادر خادمك إلى %s",
            SETTINGS.llm_provider, SETTINGS.llm_base_url,
        )
    try:
        removed = purge_old(uploads_root(), SETTINGS.session_ttl_hours * 3600)
        if removed:
            logger.info("حُذفت %d جلسة رفع قديمة.", removed)
    except OSError as exc:
        logger.warning("تعذّر تنظيف مجلد الرفع: %s", exc)
    if _CORS["open"]:
        logger.warning("=" * 62)
        logger.warning("CORS مفتوح للجميع — CODEAGENT_ALLOWED_ORIGINS فارغ.")
        logger.warning("الواجهة ستعمل، لكن أي موقع يستطيع مخاطبة خادمك.")
        logger.warning("للإغلاق: CODEAGENT_ALLOWED_ORIGINS=https://agent-mero.netlify.app")
        logger.warning("=" * 62)
    else:
        logger.info("CORS مقيّد بـ: %s%s",
                    ", ".join(_CORS["allow_origins"]) or "(أنماط فقط)",
                    f" + نمط {_CORS['allow_origin_regex']}" if _CORS["allow_origin_regex"] else "")

    if SETTINGS.memory_enabled:
        ok = await MEMORY.ping()
        if ok:
            logger.info("الذاكرة جاهزة على %s", MEMORY.backend)
        else:
            logger.error(
                "تعذّر الاتصال بقاعدة الذاكرة (%s). تحقق من CODEAGENT_MONGODB_URI "
                "ومن السماح بعنوان Railway في Atlas → Network Access.",
                MEMORY.backend,
            )
    try:
        if ACCOUNTS.count_users() == 0:
            created = ACCOUNTS.ensure_admin(SETTINGS.admin_user, SETTINGS.admin_password)
            if created:
                logger.info("أُنشئ حساب الإدارة: %s", created.username)
            else:
                logger.error("=" * 62)
                logger.error("لا توجد حسابات — لن تستطيع تسجيل الدخول من الواجهة.")
                logger.error("الحل الأول: أضف في Railway → Variables ثم أعد النشر:")
                logger.error("   CODEAGENT_ADMIN_USER=marwan")
                logger.error("   CODEAGENT_ADMIN_PASSWORD=<8 أحرف فأكثر>")
                logger.error("الحل الثاني بلا إعادة نشر: افتح الواجهة، ستعرض شاشة")
                logger.error("إنشاء أول حساب — تحتاج فيها مفتاح CODEAGENT_API_KEY.")
                logger.error("=" * 62)
        ACCOUNTS.purge_expired()
    except Exception as exc:  # noqa: BLE001
        logger.error("تعذّر تهيئة الحسابات: %s", exc)
    logger.info("جذر المشروع: %s | المزوّد: %s | نموذج الرؤية: %s",
                CFG.root, SETTINGS.llm_provider, SETTINGS.vision_model)
    yield


app = FastAPI(
    title="codeagent server",
    version=__version__,
    description="وكيل برمجي محلي مع فهم الصور — كل المعالجة تتم على خادمك.",
    lifespan=lifespan,
)

def cors_config() -> Dict[str, Any]:
    """يبني إعداد CORS مع دعم الأنماط، ولا يترك الواجهة معطّلة بصمت.

    القائمة الفارغة تعني: اسمح للجميع مع تحذير صريح. المصادقة برأس
    X-API-Key أو Bearer لا بكوكيز، فالمتصفح لا يرسلها تلقائياً من موقع آخر،
    والحماية الفعلية تبقى في المفتاح نفسه.
    """
    origins = [o.rstrip("/") for o in SETTINGS.allowed_origins if o.strip()]

    if not origins or "*" in origins:
        return {"allow_origins": ["*"], "allow_origin_regex": None, "open": True}

    exact = [o for o in origins if "*" not in o]
    patterns = [o for o in origins if "*" in o]
    regex = None
    if patterns:
        parts = [re.escape(p).replace(r"\*", r"[^/]*") for p in patterns]
        regex = "^(" + "|".join(parts) + ")$"
    return {"allow_origins": exact, "allow_origin_regex": regex, "open": False}


_CORS = cors_config()

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS["allow_origins"],
    allow_origin_regex=_CORS["allow_origin_regex"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["X-API-Key", "Content-Type", "Authorization"],
    max_age=600,
)

_RATE_BUCKET: Dict[str, Deque[float]] = defaultdict(deque)

# قاعدة الحسابات — على الجذر الرئيسي دائماً، منفصلة عن قاعدة العمليات
ACCOUNTS = Accounts(CFG.state_dir / "auth.db", session_days=SETTINGS.session_days)

# ذاكرة المحادثات: Atlas إن وُجد رابط، وإلا SQLite محلي
MEMORY: MemoryStore = build_memory(
    SETTINGS.mongodb_uri, SETTINGS.mongodb_db, CFG.state_dir / "memory.db"
)


def memory_user_id(user: Optional[User]) -> int:
    """مفتاح الذاكرة. مفتاح الخادم يُعامل كحساب مشترك برقم 0."""
    return user.id if user is not None else 0


def isolated_cfg(user: Optional[User]) -> Config:
    """مساحة عمل منفصلة لكل مستخدم عند تفعيل العزل. الإدارة ترى الجذر كاملاً."""
    if not SETTINGS.isolate_users or user is None or user.is_admin:
        return CFG
    root = (CFG.root / "users" / user.username).resolve()
    root.mkdir(parents=True, exist_ok=True)
    scoped = Config(root=root, data=CFG.data, source=CFG.source)
    scoped.ensure_state_dir()
    return scoped

UPLOADS_DIRNAME = "uploads"


def uploads_root(cfg: Optional[Config] = None) -> Path:
    """مجلد الأرشيفات المستخرجة — داخل مساحة المستخدم ليخضع لنفس حراسة المسارات."""
    target = (cfg or CFG).root / UPLOADS_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def session_dir(session_id: str, cfg: Optional[Config] = None) -> Path:
    """يتحقق أن معرّف الجلسة سليم ثم يعيد مجلدها."""
    clean = "".join(ch for ch in (session_id or "") if ch.isalnum())
    if not clean or len(clean) < 8 or len(clean) > 64:
        raise HTTPException(status_code=400, detail="معرّف جلسة غير صالح.")
    return uploads_root(cfg) / clean


def archive_limits() -> ExtractLimits:
    return ExtractLimits(
        max_files=SETTINGS.max_archive_files,
        max_total_bytes=SETTINGS.max_extracted_mb * 1024 * 1024,
    )


# --------------------------------------------------------------- الأمان

def check_rate_limit(request: Request) -> None:
    limit = SETTINGS.rate_limit_per_minute
    if limit <= 0:
        return
    client = request.client.host if request.client else "unknown"
    now = time.time()
    bucket = _RATE_BUCKET[client]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="تجاوزت الحد المسموح من الطلبات. انتظر قليلاً.")
    bucket.append(now)


async def current_user(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
) -> Optional[User]:
    """يقبل توكن جلسة (Bearer) أو مفتاح الخادم. يعيد المستخدم أو None لمفتاح الخادم."""
    check_rate_limit(request)

    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if token:
        try:
            return ACCOUNTS.resolve(token)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    if not SETTINGS.require_auth:
        return None

    # مفتاح الخادم = صلاحية إدارة كاملة (للأتمتة وcurl)
    if SETTINGS.api_key and x_api_key and hmac.compare_digest(x_api_key, SETTINGS.api_key):
        return None

    raise HTTPException(status_code=401, detail="يلزم تسجيل الدخول.")


async def require_admin(user: Optional[User] = Depends(current_user)) -> Optional[User]:
    if user is not None and not user.is_admin:
        raise HTTPException(status_code=403, detail="هذه العملية للإدارة فقط.")
    return user


def require_write(user: Optional[User]) -> None:
    if user is not None and not user.can_write:
        raise HTTPException(status_code=403, detail="حسابك للقراءة فقط. راجع الإدارة.")


def spend_quota(user: Optional[User], kind: str = "model") -> None:
    """يتحقق من الحصة قبل استدعاء النموذج ثم يسجّل الاستهلاك."""
    if user is None:
        return
    try:
        ACCOUNTS.check_quota(user, kind)
    except AuthError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    ACCOUNTS.record_usage(user.id, kind)


Guard = Depends(current_user)
AdminGuard = Depends(require_admin)


def new_session(cfg: Optional[Config] = None):
    """يفتح مخزناً ومنفّذاً لكل طلب (SQLite لا يشارك الاتصال بين الخيوط)."""
    cfg = cfg or CFG
    store = Store(cfg.db_path)
    approvals = ApprovalManager(cfg, prompter=lambda req: True, auto_approve=True)
    executor = Executor(cfg, store, approvals)
    return store, executor


def safe_log(message: str, payload: str = "") -> None:
    if SETTINGS.redact_logs:
        logger.info(message)
    else:
        logger.info("%s | %s", message, payload[:500])


async def read_images(files: List[UploadFile]) -> List[ImageInput]:
    """يقرأ الصور المرفوعة، يتحقق منها، يجرّد EXIF، ويحذفها من القرص إن لزم."""
    if not files:
        raise HTTPException(status_code=400, detail="لم تُرفَع أي صورة.")
    if len(files) > 4:
        raise HTTPException(status_code=400, detail="الحد الأقصى 4 صور في الطلب الواحد.")

    client = build_vision_client(CFG)
    max_bytes = SETTINGS.max_upload_mb * 1024 * 1024
    prepared: List[ImageInput] = []

    for upload in files:
        data = await upload.read()
        if len(data) > max_bytes:
            raise HTTPException(
                status_code=413, detail=f"حجم '{upload.filename}' يتجاوز {SETTINGS.max_upload_mb}MB."
            )
        try:
            image = client.prepare(data, name=upload.filename or "image.png")
        except ImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        prepared.append(image)

        if SETTINGS.store_uploads and SETTINGS.upload_dir:
            SETTINGS.upload_dir.mkdir(parents=True, exist_ok=True)
            target = SETTINGS.upload_dir / f"{int(time.time() * 1000)}-{Path(image.name).name}"
            target.write_bytes(image.data)

    return prepared


def validate_target(path_str: str, cfg: Optional[Config] = None) -> Path:
    """يتحقق أن المسار داخل مساحة المستخدم وغير حسّاس."""
    cfg = cfg or CFG
    try:
        resolved = resolve_in_root(cfg, path_str)
    except SafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if is_sensitive(cfg, resolved):
        raise HTTPException(
            status_code=403,
            detail=f"'{relative_path(cfg, resolved)}' ملف حسّاس — الكتابة عليه ممنوعة من الواجهة.",
        )
    return resolved


# --------------------------------------------------------------- النماذج

class ApplyFile(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    content: str = Field(max_length=2_000_000)


class ApplyRequest(BaseModel):
    files: List[ApplyFile] = Field(min_length=1, max_length=25)
    overwrite: bool = False


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    include_context: bool = True


# --------------------------------------------------------------- المسارات

@app.get("/api/health")
async def health() -> Dict[str, Any]:
    """فحص الصحة — بلا مصادقة عمداً، ولا يكشف أي سر."""
    try:
        needs_setup = ACCOUNTS.count_users() == 0
    except Exception:  # noqa: BLE001
        needs_setup = False
    try:
        llm_connected = bool(build_client(CFG.llm).is_alive())
    except Exception:
        llm_connected = False
    return {
        "status": "ok",
        "version": __version__,
        "setup_required": needs_setup,
        "llm_connected": llm_connected,
        "llm_provider": SETTINGS.llm_provider,
    }


@app.get("/api/cors-check")
async def cors_check(request: Request) -> Dict[str, Any]:
    """يخبر الواجهة هل نطاقها مسموح — بلا مصادقة، لأنه أداة تشخيص."""
    origin = request.headers.get("origin", "")
    allowed = _CORS["open"] or origin.rstrip("/") in _CORS["allow_origins"]
    if not allowed and _CORS["allow_origin_regex"] and origin:
        allowed = bool(re.match(_CORS["allow_origin_regex"], origin.rstrip("/")))
    return {
        "your_origin": origin,
        "allowed": allowed,
        "mode": "open" if _CORS["open"] else "restricted",
        "configured": _CORS["allow_origins"],
        "hint": "" if allowed else
                f"أضف في Railway: CODEAGENT_ALLOWED_ORIGINS={origin}",
    }


@app.get("/api/config", dependencies=[Guard])
async def get_config(user: Optional[User] = Guard) -> Dict[str, Any]:
    info = SETTINGS.public_dict()
    cfg = isolated_cfg(user)
    info["project_root"] = str(cfg.root)
    info["is_admin"] = user is None or user.is_admin
    if user is not None:
        info["user"] = user.public(ACCOUNTS.used_today(user.id))
    try:
        info["llm_connected"] = bool(build_client(CFG.llm).is_alive())
    except Exception:  # لا تجعل غياب النموذج يعطّل صفحة الإعدادات
        info["llm_connected"] = False
    info["git"] = git_available(CFG)
    return info


@app.get("/api/files/tree", dependencies=[Guard])
async def file_tree(user: Optional[User] = Guard) -> Dict[str, Any]:
    cfg = isolated_cfg(user)
    analysis = analyze_project(cfg)
    return {
        "root": str(cfg.root),
        "tree": analysis.tree,
        "file_count": analysis.file_count,
        "languages": analysis.languages,
        "entry_points": analysis.entry_points,
    }


@app.get("/api/files/read", dependencies=[Guard])
async def read_file(path: str, user: Optional[User] = Guard) -> Dict[str, Any]:
    cfg = isolated_cfg(user)
    target = validate_target(path, cfg)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="الملف غير موجود.")
    if is_probably_binary(target):
        raise HTTPException(status_code=415, detail="ملف ثنائي، لا يمكن عرضه كنص.")
    return {
        "path": relative_path(cfg, target),
        "content": target.read_text(encoding="utf-8", errors="replace")[:400_000],
    }


@app.post("/api/vision/describe", dependencies=[Guard])
async def vision_describe(
    images: List[UploadFile] = File(...),
    prompt: str = Form(""),
    user: Optional[User] = Guard,
) -> Dict[str, Any]:
    """يصف الصورة فقط — لا يكتب أي ملف."""
    spend_quota(user)
    prepared = await read_images(images)
    client = build_vision_client(CFG)
    try:
        client.ensure_ready()
        description = client.describe(prepared, prompt)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    store, _ = new_session()
    store.log_operation("vision_describe", {"images": len(prepared)}, "ok", "web")
    store.close()
    safe_log("وصف صورة عبر الواجهة")
    return {"description": description, "model": client.model, "images": len(prepared)}


@app.post("/api/vision/generate", dependencies=[Guard])
async def vision_generate(
    images: List[UploadFile] = File(...),
    instructions: str = Form(""),
    include_context: bool = Form(True),
    user: Optional[User] = Guard,
) -> Dict[str, Any]:
    """يحوّل الصورة إلى ملفات **مقترحة** مع معاينة الفروقات. لا يكتب شيئاً على القرص."""
    spend_quota(user)
    cfg = isolated_cfg(user)
    prepared = await read_images(images)
    client = build_vision_client(cfg)

    context = ""
    if include_context:
        context = analyze_project(cfg).to_text()[:3000]

    try:
        client.ensure_ready()
        result = client.generate_files(prepared, instructions, context)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    proposals: List[Dict[str, Any]] = []
    for item in result["files"]:
        entry: Dict[str, Any] = item.to_dict()
        try:
            resolved = resolve_in_root(cfg, item.path)
        except SafetyError as exc:
            entry.update({"allowed": False, "reason": str(exc)})
            proposals.append(entry)
            continue

        if is_sensitive(cfg, resolved):
            entry.update({"allowed": False, "reason": "الملف مصنّف حسّاس — ممنوع من الواجهة."})
            proposals.append(entry)
            continue

        rel = relative_path(cfg, resolved)
        existing = ""
        if resolved.is_file():
            existing = resolved.read_text(encoding="utf-8", errors="replace")
        added, removed = diff_stats(existing, item.content)
        entry.update(
            {
                "path": rel,
                "allowed": True,
                "exists": resolved.is_file(),
                "added": added,
                "removed": removed,
                "diff": make_diff(existing, item.content, rel),
            }
        )
        proposals.append(entry)

    store, _ = new_session()
    store.log_operation(
        "vision_generate", {"images": len(prepared), "files": len(proposals)}, "ok", "web (اقتراح فقط)"
    )
    store.close()
    safe_log(f"توليد {len(proposals)} ملف مقترح من صورة")

    return {
        "files": proposals,
        "notes": result.get("notes", ""),
        "model": client.model,
        "applied": False,
    }


@app.post("/api/files/apply", dependencies=[Guard])
async def apply_files(payload: ApplyRequest, user: Optional[User] = Guard) -> Dict[str, Any]:
    """يكتب الملفات المقترحة فعلياً — بعد ضغط المستخدم على زر التطبيق."""
    if not SETTINGS.allow_apply:
        raise HTTPException(status_code=403, detail="الكتابة معطّلة على هذا الخادم (CODEAGENT_ALLOW_APPLY=false).")
    require_write(user)
    cfg = isolated_cfg(user)
    store, executor = new_session(cfg)
    results: List[Dict[str, Any]] = []
    try:
        for item in payload.files:
            target = validate_target(item.path, cfg)
            rel = relative_path(cfg, target)
            tool = "write_file" if (target.is_file() and payload.overwrite) else "create_file"
            args: Dict[str, Any] = {"path": rel, "content": item.content}
            if tool == "create_file":
                args["overwrite"] = payload.overwrite
            result = executor.run(tool, args)
            results.append({"path": rel, "ok": result.ok, "message": result.output[:1500]})
    finally:
        store.close()

    return {"applied": sum(1 for r in results if r["ok"]), "results": results}


@app.post("/api/agent/ask", dependencies=[Guard])
async def agent_ask(payload: AskRequest, user: Optional[User] = Guard) -> Dict[str, Any]:
    """يشغّل الوكيل النصي. التعديلات تمر عبر نفس بوابة Executor وتُسجَّل."""
    spend_quota(user)
    cfg = isolated_cfg(user)
    store, executor = new_session(cfg)
    try:
        client = build_client(cfg.llm)
        client.ensure_ready()
    except LLMError as exc:
        store.close()
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    context = analyze_project(cfg).to_text()[:6000] if payload.include_context else None
    try:
        run = Agent(cfg, store, executor, client=client).run(payload.question, project_context=context)
        steps = [
            {"kind": s.kind, "name": s.name, "ok": s.ok, "text": s.text[:4000], "args": s.args}
            for s in run.steps
        ]
        return {"answer": run.answer, "steps": steps, "stopped_reason": run.stopped_reason}
    finally:
        store.close()


@app.get("/api/review", dependencies=[Guard])
async def review(path: Optional[str] = None, limit: int = 60,
                 user: Optional[User] = Guard) -> Dict[str, Any]:
    cfg = isolated_cfg(user)
    subpath = validate_target(path, cfg) if path else None
    issues = run_review(cfg, subpath=subpath, max_issues=max(1, min(limit, 300)))
    return {
        "counts": summarize_issues(issues),
        "issues": [
            {"path": i.path, "line": i.line, "severity": i.severity, "rule": i.rule, "message": i.message}
            for i in issues
        ],
    }


@app.get("/api/operations", dependencies=[Guard])
async def operations(limit: int = 20, user: Optional[User] = Guard) -> Dict[str, Any]:
    store, _ = new_session(isolated_cfg(user))
    try:
        ops = store.recent_operations(limit=max(1, min(limit, 200)))
        return {
            "operations": [
                {
                    "id": o.id,
                    "ts": o.ts,
                    "tool": o.tool,
                    "status": o.status,
                    "undone": o.undone,
                    "summary": o.summary[:300],
                }
                for o in ops
            ],
            "stats": store.stats(),
        }
    finally:
        store.close()


@app.post("/api/undo", dependencies=[Guard])
async def undo(user: Optional[User] = Guard) -> Dict[str, Any]:
    require_write(user)
    cfg = isolated_cfg(user)
    store, _ = new_session(cfg)
    try:
        op = store.last_undoable_operation()
        if op is None:
            return {"undone": False, "message": "لا توجد عمليات قابلة للتراجع."}
        result = BackupManager(cfg, store).restore_operation(op.id)
        return {
            "undone": not result.errors,
            "operation_id": op.id,
            "tool": op.tool,
            "restored": result.restored,
            "deleted": result.deleted,
            "errors": result.errors,
        }
    finally:
        store.close()



@app.get("/api/llm/check", dependencies=[Guard])
async def llm_check(user: Optional[User] = Guard) -> Dict[str, Any]:
    """يختبر الاتصال بالنموذج فعلياً برسالة قصيرة — للتحقق من المفتاح والنموذج."""
    info: Dict[str, Any] = {
        "provider": SETTINGS.llm_provider,
        "base_url": SETTINGS.llm_base_url if SETTINGS.llm_provider != "ollama" else SETTINGS.ollama_host,
        "text_model": SETTINGS.text_model,
        "vision_model": SETTINGS.vision_model,
    }
    try:
        client = build_client(CFG.llm)
        client.ensure_ready()
        started = time.time()
        response = client.chat([{"role": "user", "content": "قل: جاهز"}])
        info.update(
            {
                "ok": True,
                "latency_ms": int((time.time() - started) * 1000),
                "sample": (response.content or "")[:200],
                "tools_supported": None,
            }
        )
    except LLMError as exc:
        info.update({"ok": False, "error": str(exc)})
        return info

    # فحص ثانٍ: هل يدعم النموذج استدعاء الأدوات؟ بدونها لا يعمل الوكيل.
    try:
        probe = client.chat(
            [{"role": "user", "content": "استخدم أداة list_dir على المجلد '.'"}],
            tools=[t for t in tool_schemas() if t["function"]["name"] == "list_dir"],
        )
        info["tools_supported"] = bool(probe.tool_calls)
        if not probe.tool_calls:
            info["hint"] = (
                "النموذج ردّ نصاً بدل استدعاء الأداة. قد لا يدعم tool calling — "
                "جرّب gpt-4o-mini أو llama-3.3-70b-versatile أو qwen2.5-coder."
            )
    except LLMError as exc:
        info["tools_supported"] = False
        info["hint"] = f"فشل فحص الأدوات: {exc}"

    return info


# --------------------------------------------------------------- الحسابات

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(default="", max_length=64)
    role: str = Field(default="user")
    daily_quota: int = Field(default=100, ge=0, le=100000)
    can_write: bool = True


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=64)
    role: Optional[str] = None
    active: Optional[bool] = None
    daily_quota: Optional[int] = Field(default=None, ge=0, le=100000)
    can_write: Optional[bool] = None


class PasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)


@app.post("/api/auth/login")
async def login(payload: LoginRequest, request: Request) -> Dict[str, Any]:
    check_rate_limit(request)
    try:
        token, user = ACCOUNTS.login(
            payload.username, payload.password, request.headers.get("user-agent", "")
        )
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    logger.info("دخول: %s", user.username)
    return {
        "token": token,
        "expires_in": SETTINGS.session_days * 86400,
        "user": user.public(ACCOUNTS.used_today(user.id)),
    }


@app.post("/api/auth/bootstrap")
async def bootstrap_admin(
    payload: CreateUserRequest,
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> Dict[str, Any]:
    """ينشئ أول حساب إدارة. يعمل مرة واحدة فقط: عندما لا توجد أي حسابات.

    الحماية: يتطلب مفتاح الخادم، ويُغلق نهائياً بمجرد وجود حساب واحد.
    """
    check_rate_limit(request)

    if ACCOUNTS.count_users() > 0:
        raise HTTPException(
            status_code=409,
            detail="توجد حسابات بالفعل. استخدم تسجيل الدخول، أو أنشئ الحسابات من لوحة الإدارة.",
        )
    if not SETTINGS.api_key:
        raise HTTPException(
            status_code=500,
            detail="الخادم بلا CODEAGENT_API_KEY — لا يمكن التهيئة الآمنة.",
        )
    if not x_api_key or not hmac.compare_digest(x_api_key, SETTINGS.api_key):
        raise HTTPException(status_code=401, detail="مفتاح الخادم غير صحيح.")

    try:
        user = ACCOUNTS.create_user(
            payload.username, payload.password, role="admin",
            display_name=payload.display_name or payload.username,
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("أُنشئ حساب الإدارة الأول: %s", user.username)
    token, _ = ACCOUNTS.login(payload.username, payload.password,
                              request.headers.get("user-agent", ""))
    return {"user": user.public(), "token": token,
            "expires_in": SETTINGS.session_days * 86400}


@app.post("/api/auth/logout", dependencies=[Guard])
async def logout(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    if authorization and authorization.lower().startswith("bearer "):
        ACCOUNTS.logout(authorization[7:].strip())
    return {"ok": True}


@app.get("/api/auth/me")
async def whoami(user: Optional[User] = Guard) -> Dict[str, Any]:
    if user is None:
        return {
            "user": {
                "username": "server-key",
                "display_name": "مفتاح الخادم",
                "role": "admin",
                "active": True,
                "unlimited": True,
                "can_write": True,
            },
            "via": "api_key",
        }
    return {"user": user.public(ACCOUNTS.used_today(user.id)), "via": "session"}


@app.post("/api/auth/password", dependencies=[Guard])
async def change_own_password(
    payload: PasswordRequest, user: Optional[User] = Guard
) -> Dict[str, Any]:
    if user is None:
        raise HTTPException(status_code=400, detail="غيّر كلمة المرور من حساب مستخدم.")
    try:
        ACCOUNTS.set_password(user.id, payload.password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "تم تغيير كلمة المرور. سجّل الدخول من جديد."}


# ------------------------------ الإدارة ------------------------------

@app.get("/api/admin/users", dependencies=[AdminGuard])
async def admin_list_users() -> Dict[str, Any]:
    users = ACCOUNTS.list_users()
    for entry in users:
        entry["sessions"] = ACCOUNTS.active_sessions(entry["id"])
    return {"users": users, "total": len(users)}


@app.post("/api/admin/users", dependencies=[AdminGuard])
async def admin_create_user(payload: CreateUserRequest) -> Dict[str, Any]:
    try:
        user = ACCOUNTS.create_user(
            payload.username, payload.password, role=payload.role,
            display_name=payload.display_name, daily_quota=payload.daily_quota,
            can_write=payload.can_write,
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("أُنشئ حساب: %s (%s)", user.username, user.role)
    return {"user": user.public()}


@app.patch("/api/admin/users/{user_id}", dependencies=[AdminGuard])
async def admin_update_user(
    user_id: int, payload: UpdateUserRequest, actor: Optional[User] = AdminGuard
) -> Dict[str, Any]:
    target = ACCOUNTS.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود.")
    # لا يوقف المدير نفسه فيُقفل خارج اللوحة
    if actor is not None and actor.id == user_id and payload.active is False:
        raise HTTPException(status_code=400, detail="لا يمكنك إيقاف حسابك أنت.")
    if payload.active is False and target.is_admin:
        active_admins = sum(
            1 for u in ACCOUNTS.list_users() if u["role"] == "admin" and u["active"]
        )
        if active_admins <= 1:
            raise HTTPException(status_code=400, detail="لا يمكن إيقاف آخر حساب إدارة.")
    try:
        updated = ACCOUNTS.update_user(user_id, **payload.model_dump(exclude_none=True))
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"user": updated.public(ACCOUNTS.used_today(updated.id))}


@app.post("/api/admin/users/{user_id}/password", dependencies=[AdminGuard])
async def admin_reset_password(user_id: int, payload: PasswordRequest) -> Dict[str, Any]:
    if not ACCOUNTS.get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="المستخدم غير موجود.")
    try:
        ACCOUNTS.set_password(user_id, payload.password)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "تم تعيين كلمة مرور جديدة وإنهاء جلسات المستخدم."}


@app.delete("/api/admin/users/{user_id}", dependencies=[AdminGuard])
async def admin_delete_user(user_id: int, actor: Optional[User] = AdminGuard) -> Dict[str, Any]:
    if actor is not None and actor.id == user_id:
        raise HTTPException(status_code=400, detail="لا يمكنك حذف حسابك أنت.")
    try:
        ACCOUNTS.delete_user(user_id)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/revoke", dependencies=[AdminGuard])
async def admin_revoke_sessions(user_id: int) -> Dict[str, Any]:
    return {"revoked": ACCOUNTS.revoke_all(user_id)}


@app.get("/api/admin/usage", dependencies=[AdminGuard])
async def admin_usage() -> Dict[str, Any]:
    return {"usage": ACCOUNTS.usage_summary()}


# --------------------------------------------------------------- الذاكرة

class ConversationRequest(BaseModel):
    title: str = Field(default="", max_length=200)


class FactRequest(BaseModel):
    text: str = Field(min_length=1, max_length=400)


@app.get("/api/memory/status", dependencies=[Guard])
async def memory_status() -> Dict[str, Any]:
    alive = await MEMORY.ping()
    info: Dict[str, Any] = {"backend": MEMORY.backend, "connected": alive,
                            "enabled": SETTINGS.memory_enabled}
    if alive:
        try:
            info.update(await MEMORY.stats())
        except Exception as exc:  # noqa: BLE001
            info["error"] = str(exc)[:200]
    else:
        info["hint"] = (
            "تحقق من CODEAGENT_MONGODB_URI، ومن Atlas → Network Access "
            "(اسمح بـ 0.0.0.0/0 أو بعنوان Railway)، ومن صحة اسم المستخدم وكلمة المرور."
        )
    return info


@app.get("/api/conversations", dependencies=[Guard])
async def list_conversations(user: Optional[User] = Guard) -> Dict[str, Any]:
    convs = await MEMORY.list_conversations(memory_user_id(user))
    return {"conversations": [c.public() for c in convs]}


@app.post("/api/conversations", dependencies=[Guard])
async def create_conversation(
    payload: ConversationRequest, user: Optional[User] = Guard
) -> Dict[str, Any]:
    conv = await MEMORY.create_conversation(memory_user_id(user), payload.title)
    return {"conversation": conv.public()}


@app.get("/api/conversations/{conversation_id}", dependencies=[Guard])
async def get_conversation(conversation_id: str, user: Optional[User] = Guard) -> Dict[str, Any]:
    uid = memory_user_id(user)
    conv = await MEMORY.get_conversation(conversation_id, uid)
    if conv is None:
        raise HTTPException(status_code=404, detail="المحادثة غير موجودة.")
    messages = await MEMORY.get_messages(conversation_id, uid, limit=300)
    return {
        "conversation": conv.public(),
        "summary": conv.summary,
        "messages": [m.public() for m in messages],
    }


@app.patch("/api/conversations/{conversation_id}", dependencies=[Guard])
async def rename_conversation(
    conversation_id: str, payload: ConversationRequest, user: Optional[User] = Guard
) -> Dict[str, Any]:
    ok = await MEMORY.rename_conversation(conversation_id, memory_user_id(user), payload.title)
    if not ok:
        raise HTTPException(status_code=404, detail="المحادثة غير موجودة.")
    return {"ok": True}


@app.delete("/api/conversations/{conversation_id}", dependencies=[Guard])
async def delete_conversation(conversation_id: str, user: Optional[User] = Guard) -> Dict[str, Any]:
    ok = await MEMORY.delete_conversation(conversation_id, memory_user_id(user))
    if not ok:
        raise HTTPException(status_code=404, detail="المحادثة غير موجودة.")
    return {"ok": True}


@app.get("/api/memory/facts", dependencies=[Guard])
async def list_facts(user: Optional[User] = Guard) -> Dict[str, Any]:
    return {"facts": await MEMORY.get_facts(memory_user_id(user))}


@app.post("/api/memory/facts", dependencies=[Guard])
async def add_fact(payload: FactRequest, user: Optional[User] = Guard) -> Dict[str, Any]:
    try:
        fact = await MEMORY.add_fact(memory_user_id(user), payload.text, source="manual")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"fact": fact}


@app.delete("/api/memory/facts/{fact_id}", dependencies=[Guard])
async def delete_fact(fact_id: str, user: Optional[User] = Guard) -> Dict[str, Any]:
    ok = await MEMORY.delete_fact(memory_user_id(user), fact_id)
    if not ok:
        raise HTTPException(status_code=404, detail="الحقيقة غير موجودة.")
    return {"ok": True}


# --------------------------------------------------------------- الدردشة العامة

CHAT_SYSTEM = """أنت مساعد ذكي يتحدث بلغة المستخدم. تجيب بدقة وإيجاز.
قد يرفق المستخدم صوراً أو ملفات نصية — استخدمها في إجابتك.
إذا طلب منك إنشاء ملف فعلي على الخادم، أخبره أن يستخدم تبويب الوكيل الذي يملك أدوات الكتابة."""

MAX_CHAT_FILE_CHARS = 60_000
MAX_CHAT_HISTORY = 24


@app.post("/api/chat", dependencies=[Guard])
async def chat(
    message: str = Form(""),
    history: str = Form("[]"),
    conversation_id: str = Form(""),
    images: List[UploadFile] = File(default=[]),
    files: List[UploadFile] = File(default=[]),
    use_tools: bool = Form(False),
    user: Optional[User] = Guard,
) -> Dict[str, Any]:
    """دردشة عامة مع النموذج: نص + صور + ملفات نصية مرفقة.

    use_tools=true يمنح النموذج أدوات الوكيل داخل المحادثة نفسها.
    """
    uid = memory_user_id(user)
    conv = None
    memory_on = SETTINGS.memory_enabled

    messages: List[Dict[str, Any]] = [{"role": "system", "content": CHAT_SYSTEM}]

    if memory_on:
        # الذاكرة المخزّنة هي المصدر — لا نثق بتاريخ المتصفح
        if conversation_id:
            conv = await MEMORY.get_conversation(conversation_id, uid)
            if conv is None:
                raise HTTPException(status_code=404, detail="المحادثة غير موجودة.")
        else:
            conv = await MEMORY.create_conversation(uid, message)

        ctx = await MEMORY.build_context(conv.id, uid, keep_recent=SETTINGS.memory_keep_recent)
        if ctx["system"]:
            messages.append({"role": "system", "content": ctx["system"]})
        messages.extend(ctx["messages"])
    else:
        try:
            past = json.loads(history or "[]")
            if not isinstance(past, list):
                past = []
        except json.JSONDecodeError:
            past = []
        for item in past[-MAX_CHAT_HISTORY:]:
            role = item.get("role")
            content = str(item.get("content", ""))[:20000]
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

    # الملفات النصية تُقرأ وتُدمج في نص الرسالة
    attached: List[str] = []
    parts: List[str] = [message.strip()] if message.strip() else []
    for upload in (files or [])[:6]:
        raw = await upload.read()
        if len(raw) > 1024 * 1024:
            attached.append(f"{upload.filename} (تخطّي: أكبر من 1MB)")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            attached.append(f"{upload.filename} (تخطّي: ليس ملفاً نصياً)")
            continue
        if len(text) > MAX_CHAT_FILE_CHARS:
            text = text[:MAX_CHAT_FILE_CHARS] + "\n... [مقتطع]"
        parts.append(f"\n--- محتوى الملف: {upload.filename} ---\n{text}")
        attached.append(upload.filename or "file")

    # الصور
    encoded_images: List[str] = []
    if images:
        client_v = build_vision_client(isolated_cfg(user))
        for upload in images[:4]:
            raw = await upload.read()
            if len(raw) > SETTINGS.max_upload_mb * 1024 * 1024:
                raise HTTPException(status_code=413, detail=f"صورة أكبر من {SETTINGS.max_upload_mb}MB.")
            try:
                prepared = client_v.prepare(raw, name=upload.filename or "image.png")
            except ImageError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            encoded_images.append(prepared.encoded())

    if not parts and not encoded_images:
        raise HTTPException(status_code=400, detail="أرسل رسالة أو مرفقاً على الأقل.")

    user_msg: Dict[str, Any] = {"role": "user", "content": "\n".join(parts) or "صف هذه الصور."}
    if encoded_images:
        user_msg["images"] = encoded_images
    messages.append(user_msg)

    spend_quota(user)
    if use_tools:
        require_write(user)
    cfg = isolated_cfg(user)
    try:
        client = build_client(cfg.llm)
        client.ensure_ready()
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def remember(reply: str, tool_names: List[str]) -> None:
        """يحفظ الدور كاملاً ثم يشغّل التلخيص واستخراج الحقائق عند الحاجة."""
        if not memory_on or conv is None:
            return
        await MEMORY.add_message(conv.id, uid, "user", "\n".join(parts) or "(مرفقات)",
                                 attachments=attached + [f"صورة×{len(encoded_images)}"]
                                 if encoded_images else attached)
        await MEMORY.add_message(conv.id, uid, "assistant", reply, tool_names=tool_names)

        fresh = await MEMORY.get_conversation(conv.id, uid)
        if fresh is None:
            return

        keep = SETTINGS.memory_keep_recent
        if MEMORY.needs_summary(fresh, keep, SETTINGS.memory_summary_trigger):
            older = await MEMORY.get_messages(
                conv.id, uid, limit=fresh.message_count - keep, skip=fresh.summarized_upto
            )
            if older:
                try:
                    text = summarize_messages(client, older, fresh.summary)
                    await MEMORY.set_summary(conv.id, uid, text, fresh.message_count - keep)
                except LLMError as exc:
                    logger.warning("فشل التلخيص: %s", exc)

        every = max(2, SETTINGS.memory_facts_every)
        if fresh.message_count % every == 0:
            recent = await MEMORY.get_messages(
                conv.id, uid, limit=every, skip=max(0, fresh.message_count - every)
            )
            for fact in extract_facts(client, recent):
                try:
                    await MEMORY.add_fact(uid, fact, source="auto")
                except ValueError:
                    continue

    store, executor = new_session(cfg)
    try:
        if not use_tools:
            try:
                response = client.chat(messages)
            except LLMError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            store.log_operation("chat", {"images": len(encoded_images), "files": len(attached)}, "ok", "دردشة")
            await remember(response.content or "", [])
            return {
                "reply": response.content,
                "attached": attached,
                "images": len(encoded_images),
                "steps": [],
                "conversation_id": conv.id if conv else None,
            }

        # مع الأدوات: حلقة مصغّرة تمر بنفس بوابة Executor
        steps: List[Dict[str, Any]] = []
        for _ in range(int(cfg.llm.get("max_steps", 12))):
            try:
                response = client.chat(messages, tools=tool_schemas())
            except LLMError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

            if not response.tool_calls:
                store.log_operation("chat_tools", {"steps": len(steps)}, "ok", "دردشة بأدوات")
                await remember(response.content or "", [s["name"] for s in steps])
                return {"reply": response.content, "attached": attached,
                        "images": len(encoded_images), "steps": steps,
                        "conversation_id": conv.id if conv else None}

            for index, call in enumerate(response.tool_calls):
                if not call.id:
                    call.id = f"call_{len(steps)}_{index}"
            messages.append({
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {"id": c.id, "function": {"name": c.name, "arguments": c.arguments}}
                    for c in response.tool_calls
                ],
            })
            for call in response.tool_calls:
                result = executor.run(call.name, call.arguments or {})
                steps.append({
                    "kind": "tool", "name": call.name, "ok": result.ok,
                    "args": call.arguments or {}, "text": result.output[:4000],
                })
                messages.append({
                    "role": "tool", "name": call.name, "tool_call_id": call.id,
                    "content": ("" if result.ok else "فشل: ") + result.output[:12000],
                })

        await remember("توقفت بعد الحد الأقصى من الخطوات.", [s["name"] for s in steps])
        return {"reply": "توقفت بعد الحد الأقصى من الخطوات.", "attached": attached,
                "images": len(encoded_images), "steps": steps,
                "conversation_id": conv.id if conv else None}
    finally:
        store.close()


# --------------------------------------------------------------- الأرشيفات

@app.post("/api/archive/upload", dependencies=[Guard])
async def upload_archive(file: UploadFile = File(...), user: Optional[User] = Guard) -> Dict[str, Any]:
    """يرفع أرشيفاً ويستخرجه داخل uploads/<session> ثم يعيد شجرته."""
    name = (file.filename or "").lower()
    if not name.endswith((".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2")):
        raise HTTPException(status_code=400, detail="الصيغ المقبولة: zip, tar, tar.gz, tgz")

    require_write(user)
    cfg = isolated_cfg(user)
    session = uuid.uuid4().hex[:16]
    target_dir = session_dir(session, cfg)
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = SETTINGS.max_archive_mb * 1024 * 1024
    temp = target_dir.parent / f".{session}.upload"
    size = 0
    try:
        with temp.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"حجم الأرشيف يتجاوز {SETTINGS.max_archive_mb}MB.",
                    )
                out.write(chunk)

        result = extract_archive(temp, target_dir, archive_limits())
    except ArchiveError as exc:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    finally:
        temp.unlink(missing_ok=True)

    store, _ = new_session(cfg)
    store.log_operation(
        "archive_upload",
        {"session": session, "files": result.count},
        "ok",
        f"استُخرج {result.count} ملف في uploads/{session}",
    )
    store.close()
    safe_log(f"استُخرج أرشيف: {result.count} ملف")

    tree = sorted(result.files)[:400]
    return {
        "session": session,
        "path": f"{UPLOADS_DIRNAME}/{session}",
        "files": result.count,
        "bytes": result.total_bytes,
        "skipped": result.skipped[:50],
        "tree": tree,
    }


@app.get("/api/archive/sessions", dependencies=[Guard])
async def list_sessions(user: Optional[User] = Guard) -> Dict[str, Any]:
    base = uploads_root(isolated_cfg(user))
    items = []
    for child in sorted(base.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        files = sum(1 for p in child.rglob("*") if p.is_file())
        items.append(
            {
                "session": child.name,
                "path": f"{UPLOADS_DIRNAME}/{child.name}",
                "files": files,
                "modified": child.stat().st_mtime,
            }
        )
    return {"sessions": items[:50]}


@app.post("/api/archive/purge", dependencies=[Guard])
async def purge_sessions(hours: Optional[int] = None, user: Optional[User] = Guard) -> Dict[str, Any]:
    ttl = int(hours if hours is not None else SETTINGS.session_ttl_hours)
    removed = purge_old(uploads_root(isolated_cfg(user)), max(0, ttl) * 3600)
    return {"removed": removed, "older_than_hours": ttl}


@app.get("/api/files/download", dependencies=[Guard])
async def download_file(path: str, user: Optional[User] = Guard):
    """ينزّل ملفاً واحداً من داخل مساحة المستخدم."""
    target = validate_target(path, isolated_cfg(user))
    if not target.is_file():
        raise HTTPException(status_code=404, detail="الملف غير موجود.")
    return FileResponse(
        target,
        filename=target.name,
        media_type="application/octet-stream",
    )


@app.get("/api/files/download-zip", dependencies=[Guard])
async def download_zip(path: str, user: Optional[User] = Guard):
    """يحزم مجلداً من داخل مساحة المستخدم في ZIP وينزّله."""
    target = validate_target(path, isolated_cfg(user))
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="المسار ليس مجلداً.")
    output = Path("/tmp") / f"codeagent-{uuid.uuid4().hex[:10]}.zip"
    try:
        make_zip(target, output)
    except ArchiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(output, filename=f"{target.name or 'project'}.zip", media_type="application/zip")


# --------------------------------------------------------------- الواجهة

@app.get("/")
async def index():
    page = FRONTEND_DIR / "index.html"
    if not page.is_file():
        return JSONResponse({"detail": "ملف الواجهة غير موجود.", "expected": str(page)}, status_code=404)
    return FileResponse(page, media_type="text/html")


