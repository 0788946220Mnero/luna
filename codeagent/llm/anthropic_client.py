"""عميل Claude API (Anthropic Messages).

يختلف عن OpenAI في خمسة أمور جوهرية:
  1. المصادقة برأس x-api-key لا Authorization: Bearer
  2. رأس anthropic-version إلزامي
  3. رسالة النظام معامل مستقل system وليست ضمن messages
  4. الأدوات تستخدم input_schema بدل parameters
  5. نتائج الأدوات تُرسل في رسالة user كـ tool_result blocks
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import requests

from .ollama_client import LLMError, LLMResponse, ToolCall

API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"


def _split_system(messages: List[Dict[str, Any]]) -> tuple:
    """يفصل رسائل النظام عن بقية المحادثة."""
    system_parts: List[str] = []
    rest: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                system_parts.append(content)
        else:
            rest.append(msg)
    return "\n\n".join(system_parts), rest


def _to_anthropic(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """يحوّل الصيغة الداخلية إلى صيغة Anthropic."""
    out: List[Dict[str, Any]] = []
    pending_results: List[Dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for msg in messages:
        role = msg.get("role")

        if role == "tool":
            # نتائج الأدوات تتجمّع ثم تُرسل دفعة واحدة في رسالة user
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id") or "call_0",
                    "content": str(msg.get("content", ""))[:100_000],
                }
            )
            continue

        flush_results()

        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            text = msg.get("content")
            if isinstance(text, str) and text.strip():
                blocks.append({"type": "text", "text": text})
            for index, call in enumerate(msg.get("tool_calls") or []):
                func = call.get("function", {}) or {}
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except json.JSONDecodeError:
                        args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or f"call_{index}",
                        "name": func.get("name", ""),
                        "input": args if isinstance(args, dict) else {},
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            continue

        # user
        content = msg.get("content", "")
        if msg.get("images"):
            blocks = []
            for encoded in msg["images"]:
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": encoded},
                    }
                )
            if isinstance(content, str) and content.strip():
                blocks.append({"type": "text", "text": content})
            out.append({"role": "user", "content": blocks})
        else:
            out.append({"role": "user", "content": content if content else "..."})

    flush_results()
    return out


def _tools_to_anthropic(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    for tool in tools or []:
        func = tool.get("function", tool) or {}
        converted.append(
            {
                "name": func.get("name", ""),
                "description": (func.get("description") or "")[:1000],
                "input_schema": func.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


class AnthropicClient:
    """عميل Claude API للنص والرؤية والأدوات."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        model: str = DEFAULT_MODEL,
        temperature: float = 0.1,
        timeout: int = 300,
        max_tokens: int = 8192,
        max_dimension: int = 1600,
        strip_exif: bool = True,
    ):
        if not api_key:
            raise LLMError("مفتاح Claude غير مضبوط. عيّن CODEAGENT_LLM_API_KEY.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.max_dimension = max_dimension
        self.strip_exif = strip_exif

    # -------------------------------------------------------- فحوصات
    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    def is_alive(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/v1/models", headers=self._headers(), timeout=10)
            return resp.status_code < 500
        except requests.RequestException:
            return False

    def list_models(self) -> List[str]:
        try:
            resp = requests.get(f"{self.base_url}/v1/models", headers=self._headers(), timeout=15)
            resp.raise_for_status()
            return [m.get("id", "") for m in resp.json().get("data", [])]
        except requests.RequestException as exc:
            raise LLMError(f"تعذّر الوصول إلى Claude API: {exc}") from exc

    def ensure_ready(self) -> None:
        if not self.api_key:
            raise LLMError("مفتاح Claude غير مضبوط (CODEAGENT_LLM_API_KEY).")

    # -------------------------------------------------------- المحادثة
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        system, rest = _split_system(messages)
        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": _to_anthropic(rest),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = _tools_to_anthropic(tools)

        try:
            resp = requests.post(
                f"{self.base_url}/v1/messages",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise LLMError(f"انتهت مهلة انتظار Claude ({self.timeout}s).") from exc
        except requests.RequestException as exc:
            raise LLMError(f"فشل الاتصال بـ Claude API: {exc}") from exc

        if resp.status_code == 401:
            raise LLMError("مفتاح Claude مرفوض (401). تحقق من CODEAGENT_LLM_API_KEY.")
        if resp.status_code == 429:
            raise LLMError("تجاوزت حد الطلبات أو الرصيد (429).")
        if resp.status_code >= 400:
            detail = resp.text[:400]
            if "model" in detail.lower():
                raise LLMError(
                    f"النموذج '{self.model}' غير مقبول. جرّب claude-sonnet-5 أو claude-haiku-4-5.\n{detail}"
                )
            raise LLMError(f"خطأ من Claude API ({resp.status_code}): {detail}")

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError("استجابة Claude ليست JSON صالحاً.") from exc

        text_parts: List[str] = []
        calls: List[ToolCall] = []
        for block in data.get("content", []) or []:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "tool_use":
                raw = block.get("input", {})
                calls.append(
                    ToolCall(
                        name=block.get("name", ""),
                        arguments=raw if isinstance(raw, dict) else {},
                        id=block.get("id", ""),
                    )
                )

        return LLMResponse(content="\n".join(text_parts).strip(), tool_calls=calls, raw=data)

    # -------------------------------------------------------- الرؤية
    def prepare(self, data: bytes, name: str = "image.png"):
        from ..vision import ImageInput, sanitize_image

        return ImageInput(data=sanitize_image(data, self.max_dimension, self.strip_exif), name=name)

    def describe(self, images, prompt: str = "") -> str:
        from ..vision import DESCRIBE_SYSTEM, ImageError

        if not images:
            raise ImageError("لم تُرفَع أي صورة.")
        response = self.chat(
            [
                {"role": "system", "content": DESCRIBE_SYSTEM},
                {
                    "role": "user",
                    "content": prompt.strip() or "صف هذه الصورة بتفصيل تقني.",
                    "images": [img.encoded() for img in images],
                },
            ]
        )
        return response.content

    def generate_files(self, images, instructions: str, project_context: str = "") -> Dict[str, Any]:
        from ..vision import GENERATE_SYSTEM, ImageError, parse_generation

        if not images:
            raise ImageError("لم تُرفَع أي صورة.")
        parts = [instructions.strip() or "حوّل هذه الصورة إلى كود جاهز."]
        if project_context:
            parts.append(f"\nسياق المشروع:\n{project_context[:3000]}")
        response = self.chat(
            [
                {"role": "system", "content": GENERATE_SYSTEM},
                {
                    "role": "user",
                    "content": "\n".join(parts),
                    "images": [img.encoded() for img in images],
                },
            ]
        )
        return parse_generation(response.content)
