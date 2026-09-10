#!/bin/sh
# إقلاع الخادم على Railway: فحوصات صريحة برسائل مفهومة.
# المبدأ: كل فشل يشرح نفسه ويقترح الإصلاح، ولا ينهار بأثر غامض.
set -e

log()  { echo "[codeagent] $1"; }
fail() { echo "[codeagent] ❌ $1"; }

PORT="${PORT:-8080}"

# ═══════════ 1. جذر المشروع ═══════════
ROOT="${CODEAGENT_PROJECT_ROOT:-/data/workspace}"

# القيم المثال التي تُنسخ من ملفات الإعداد دون تعديل
case "$ROOT" in
  /absolute/*|*/path/to/*|*your-project*|*المسار*)
    fail "CODEAGENT_PROJECT_ROOT ما زال على القيمة المثال: $ROOT"
    fail "هذه قيمة توضيحية من ملف الإعدادات، وليست مساراً حقيقياً."
    log  "الإصلاح: Railway → Variables → CODEAGENT_PROJECT_ROOT=/data/workspace"
    log  "وتأكد من Settings → Volumes → قرص مركّب على /data"
    ROOT="/data/workspace"
    log  "سأتابع مؤقتاً على $ROOT — صحّح المتغير لتثبيت البيانات."
    ;;
esac

# إنشاء المجلد مع بدائل بدل الانهيار
if ! mkdir -p "$ROOT" 2>/dev/null; then
  fail "تعذّر إنشاء مجلد المشروع: $ROOT"
  fail "الأرجح أن المسار خارج القرص المركّب أو بلا صلاحية كتابة."
  if mkdir -p /data/workspace 2>/dev/null; then
    ROOT="/data/workspace"
    log "التحويل إلى $ROOT"
  elif mkdir -p /tmp/workspace 2>/dev/null; then
    ROOT="/tmp/workspace"
    log "تحذير: $ROOT مؤقت ويُمحى مع كل إعادة نشر."
    log "       أضف قرصاً على /data من Settings → Volumes."
  else
    fail "تعذّر إنشاء أي مجلد عمل. توقّف."
    exit 1
  fi
fi

if [ ! -w "$ROOT" ]; then
  fail "لا صلاحية كتابة على $ROOT"
  log  "تأكد أن القرص مركّب على /data وأن الحاوية تعمل بالمستخدم الصحيح."
  exit 1
fi
export CODEAGENT_PROJECT_ROOT="$ROOT"

# ═══════════ 2. مفتاح الواجهة ═══════════
if [ "${CODEAGENT_REQUIRE_AUTH:-true}" = "true" ]; then
  if [ -z "${CODEAGENT_API_KEY}" ]; then
    fail "CODEAGENT_API_KEY فارغ والخادم عام على الإنترنت."
    log  "ولّده بـ: openssl rand -base64 32"
    exit 1
  fi
  KEY_LEN=$(printf '%s' "${CODEAGENT_API_KEY}" | wc -c)
  if [ "${KEY_LEN}" -lt 24 ]; then
    fail "مفتاح API قصير (${KEY_LEN} حرفاً). الحد الأدنى 24."
    exit 1
  fi
  case "${CODEAGENT_API_KEY}" in
    *مفتاح*|*عشوائي*|changeme*|test*|secret|password|123*)
      fail "CODEAGENT_API_KEY ما زال على القيمة المثال. غيّره."
      exit 1 ;;
  esac
fi

# ═══════════ 3. حساب الإدارة ═══════════
case "${CODEAGENT_ADMIN_PASSWORD}" in
  *غيّرها*|*كلمة-مرور-قوية*)
    fail "CODEAGENT_ADMIN_PASSWORD ما زال على القيمة المثال."
    log  "ضع كلمة مرور حقيقية — بها ستدخل إلى الواجهة."
    exit 1 ;;
esac
if [ -n "${CODEAGENT_ADMIN_USER}" ] && [ -z "${CODEAGENT_ADMIN_PASSWORD}" ]; then
  fail "CODEAGENT_ADMIN_USER مضبوط لكن كلمة المرور فارغة — لن يُنشأ حساب الإدارة."
  exit 1
fi
if [ -z "${CODEAGENT_ADMIN_USER}" ] && [ -z "${CODEAGENT_ADMIN_PASSWORD}" ]; then
  log "تنبيه: CODEAGENT_ADMIN_USER و CODEAGENT_ADMIN_PASSWORD فارغان."
  log "       إن لم توجد حسابات، ستعرض الواجهة شاشة إنشاء أول حساب"
  log "       وتحتاج فيها مفتاح CODEAGENT_API_KEY."
fi
if [ -n "${CODEAGENT_ADMIN_PASSWORD}" ]; then
  PW_LEN=$(printf '%s' "${CODEAGENT_ADMIN_PASSWORD}" | wc -c)
  if [ "${PW_LEN}" -lt 8 ]; then
    fail "CODEAGENT_ADMIN_PASSWORD أقصر من 8 أحرف — سيفشل إنشاء الحساب بصمت."
    exit 1
  fi
fi

# ═══════════ 4. مزوّد النموذج ═══════════
case "${CODEAGENT_LLM_PROVIDER:-openai}" in
  openai|api|groq|openrouter|anthropic|claude)
    if [ -z "${CODEAGENT_LLM_API_KEY}" ]; then
      fail "CODEAGENT_LLM_API_KEY فارغ والمزوّد الخارجي مفعّل."
      exit 1
    fi
    case "${CODEAGENT_LLM_API_KEY}" in
      "sk-ant-..."|"sk-..."|*ضع*)
        fail "CODEAGENT_LLM_API_KEY ما زال على القيمة المثال."
        exit 1 ;;
    esac
    log "المزوّد: ${CODEAGENT_LLM_PROVIDER:-openai}"
    log "تنبيه: محتوى الملفات والصور سيغادر الخادم إلى هذا المزوّد."
    ;;
  ollama)
    log "المزوّد: ollama — تنبيه: Railway بلا GPU، النماذج المحلية ستفشل أو تبطؤ."
    ;;
esac

# ═══════════ 5. نطاق الواجهة ═══════════
if [ -z "${CODEAGENT_ALLOWED_ORIGINS}" ]; then
  log "تحذير: CODEAGENT_ALLOWED_ORIGINS فارغ — واجهة Netlify لن تستطيع الاتصال."
  log "        أضف نطاق موقعك، مثل: https://agent-mero.netlify.app"
else
  case "${CODEAGENT_ALLOWED_ORIGINS}" in
    *اسم-موقعك*|*your-site*)
      log "تحذير: CODEAGENT_ALLOWED_ORIGINS يبدو قيمة مثال: ${CODEAGENT_ALLOWED_ORIGINS}" ;;
    */)
      log "تحذير: أزل الشرطة المائلة من نهاية CODEAGENT_ALLOWED_ORIGINS" ;;
  esac
  log "النطاقات المسموحة: ${CODEAGENT_ALLOWED_ORIGINS}"
fi

# ═══════════ 6. الذاكرة ═══════════
if [ -n "${CODEAGENT_MONGODB_URI}" ]; then
  case "${CODEAGENT_MONGODB_URI}" in
    *"<password>"*|*user:pass@*)
      fail "CODEAGENT_MONGODB_URI ما زال يحتوي قيمة المثال."
      log  "استبدلها ببيانات Atlas الحقيقية."
      exit 1 ;;
    mongodb*) log "الذاكرة: MongoDB Atlas" ;;
    *) fail "CODEAGENT_MONGODB_URI لا يبدأ بـ mongodb:// أو mongodb+srv://" ; exit 1 ;;
  esac
else
  log "الذاكرة: SQLite محلي (لم يُضبط CODEAGENT_MONGODB_URI)"
fi

# ═══════════ الإقلاع ═══════════
log "جذر المشروع: ${ROOT}"
log "المنفذ: ${PORT}"
log "بدء التشغيل…"

exec python -m uvicorn server.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --proxy-headers \
  --forwarded-allow-ips '*' \
  --timeout-keep-alive 75 \
  --log-level "${LOG_LEVEL:-info}"
