"""عميل Ollama المحلي (يدعم استدعاء الأدوات)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests


class LLMError(Exception):
    """خطأ في الاتصال بالنموذج أو في استجابته."""


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass
class LLMResponse:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


class OllamaClient:
    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "qwen2.5-coder:7b",
        temperature: float = 0.1,
        num_ctx: int = 8192,
        timeout: int = 300,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.timeout = timeout

    # ------------------------------------------------------ فحوصات
    def is_alive(self) -> bool:
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=5)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def list_models(self) -> List[str]:
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=10)
            resp.raise_for_status()
            return [m.get("name", "") for m in resp.json().get("models", [])]
        except requests.RequestException as exc:
            raise LLMError(f"تعذّر الاتصال بـ Ollama على {self.host}: {exc}") from exc

    def ensure_ready(self) -> None:
        if not self.is_alive():
            raise LLMError(
                f"Ollama غير متاح على {self.host}.\n"
                "شغّله بالأمر: ollama serve\n"
                "ثم نزّل نموذجاً: ollama pull qwen2.5-coder:7b"
            )
        models = self.list_models()
        base = self.model.split(":")[0]
        if models and not any(m == self.model or m.split(":")[0] == base for m in models):
            raise LLMError(
                f"النموذج '{self.model}' غير موجود محلياً.\n"
                f"النماذج المتاحة: {', '.join(models) or 'لا شيء'}\n"
                f"نزّله بالأمر: ollama pull {self.model}"
            )

    # ------------------------------------------------------ المحادثة
    @staticmethod
    def _clean(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """يزيل حقول صيغة OpenAI التي لا يعرفها Ollama."""
        cleaned: List[Dict[str, Any]] = []
        for msg in messages:
            entry = {k: v for k, v in msg.items() if k != "tool_call_id"}
            if entry.get("tool_calls"):
                entry["tool_calls"] = [
                    {"function": {
                        "name": (c.get("function") or {}).get("name", ""),
                        "arguments": (c.get("function") or {}).get("arguments", {}),
                    }}
                    for c in entry["tool_calls"]
                ]
            cleaned.append(entry)
        return cleaned

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        messages = self._clean(messages)
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        if tools:
            payload["tools"] = tools

        try:
            resp = requests.post(
                f"{self.host}/api/chat", json=payload, timeout=self.timeout
            )
        except requests.Timeout as exc:
            raise LLMError(f"انتهت مهلة انتظار النموذج ({self.timeout}s).") from exc
        except requests.RequestException as exc:
            raise LLMError(f"فشل الاتصال بـ Ollama: {exc}") from exc

        if resp.status_code != 200:
            detail = resp.text[:500]
            if "does not support tools" in detail or "tools" in detail and resp.status_code == 400:
                raise LLMError(
                    f"النموذج '{self.model}' لا يدعم استدعاء الأدوات. "
                    "استخدم نموذجاً يدعمها مثل qwen2.5-coder أو llama3.1 أو mistral-nemo."
                )
            raise LLMError(f"استجابة خطأ من Ollama ({resp.status_code}): {detail}")

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError("استجابة Ollama ليست JSON صالحاً.") from exc

        message = data.get("message", {}) or {}
        calls: List[ToolCall] = []
        for item in message.get("tool_calls", []) or []:
            func = item.get("function", {}) or {}
            name = func.get("name", "")
            raw_args = func.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    raw_args = {"_raw": raw_args}
            if not isinstance(raw_args, dict):
                raw_args = {"_raw": raw_args}
            if name:
                calls.append(ToolCall(name=name, arguments=raw_args))

        return LLMResponse(content=message.get("content", "") or "", tool_calls=calls, raw=data)


class MockClient:
    """عميل وهمي للاختبار دون الحاجة إلى Ollama."""

    def __init__(self, scripted: Optional[List[LLMResponse]] = None, **_: Any):
        self.model = "mock"
        self.scripted = scripted or []
        self.calls: List[List[Dict[str, Any]]] = []

    def is_alive(self) -> bool:
        return True

    def list_models(self) -> List[str]:
        return ["mock"]

    def ensure_ready(self) -> None:
        return None

    def chat(self, messages, tools=None) -> LLMResponse:
        self.calls.append(messages)
        if self.scripted:
            return self.scripted.pop(0)
        return LLMResponse(content="(نموذج وهمي) لا توجد استجابة مبرمجة.", tool_calls=[])


def build_client(cfg_llm: Dict[str, Any]):
    provider = str(cfg_llm.get("provider", "openai")).lower()
    if provider == "mock":
        return MockClient()
    if provider in ("anthropic", "claude"):
        from .anthropic_client import AnthropicClient

        return AnthropicClient(
            api_key=cfg_llm.get("api_key", ""),
            base_url=cfg_llm.get("base_url") or "https://api.anthropic.com",
            model=cfg_llm.get("model", "claude-sonnet-5"),
            temperature=float(cfg_llm.get("temperature", 0.1)),
            timeout=int(cfg_llm.get("timeout", 300)),
        )
    if provider in ("openai", "api", "groq", "openrouter"):
        from .openai_client import OpenAICompatClient

        return OpenAICompatClient(
            api_key=cfg_llm.get("api_key", ""),
            base_url=cfg_llm.get("base_url", "https://api.openai.com/v1"),
            model=cfg_llm.get("model", "gpt-5.6-sol"),
            temperature=float(cfg_llm.get("temperature", 0.1)),
            timeout=int(cfg_llm.get("timeout", 180)),
        )
    if provider != "ollama":
        raise LLMError(f"مزوّد غير مدعوم: {provider}. المدعوم: anthropic, openai, ollama, mock")
    return OllamaClient(
        host=cfg_llm.get("host", "http://localhost:11434"),
        model=cfg_llm.get("model", "qwen2.5-coder:7b"),
        temperature=float(cfg_llm.get("temperature", 0.1)),
        num_ctx=int(cfg_llm.get("num_ctx", 8192)),
        timeout=int(cfg_llm.get("timeout", 300)),
    )
