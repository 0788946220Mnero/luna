# ============================================================
#  صورة الباك اند للنشر على Railway
#  Railway يبني من هذا الملف ويمرر متغير PORT تلقائياً.
# ============================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates libjpeg62-turbo zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY codeagent/ /app/codeagent/
COPY server/    /app/server/
COPY frontend/  /app/frontend/
COPY railway/start.sh /app/start.sh

RUN chmod +x /app/start.sh \
    && useradd -u 1000 -m agent \
    && mkdir -p /data/workspace \
    && chown -R agent:agent /app /data

USER agent

ENV CODEAGENT_PROJECT_ROOT=/data/workspace \
    CODEAGENT_HOST=0.0.0.0 \
    CODEAGENT_LLM_PROVIDER=openai \
    GIT_CONFIG_COUNT=3 \
    GIT_CONFIG_KEY_0=safe.directory \
    GIT_CONFIG_VALUE_0=/data/workspace \
    GIT_CONFIG_KEY_1=user.email \
    GIT_CONFIG_VALUE_1=agent@codeagent \
    GIT_CONFIG_KEY_2=user.name \
    GIT_CONFIG_VALUE_2=codeagent

EXPOSE 8080
CMD ["/app/start.sh"]
