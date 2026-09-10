"""عميل متوافق مع OpenAI — يعمل مع OpenAI و Groq و OpenRouter و Together وغيرها.

لا يعتمد على حزمة openai: طلبات HTTP مباشرة عبر requests لتقليل التبعيات.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import requests

from .ollama_client import LLMError, LLMResponse, ToolCall


def _as_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """يحوّل الصيغة الداخلية إلى صيغة OpenAI (arguments كنص، tool_call_id إلزامي)."""
    out: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            calls = []
            for index, call in enumerate(msg["tool_calls"]):
                func = call.get("function", {}) or {}
                args = func.get("arguments", {})
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                calls.append(
                    {
                        "id": call.get("id") or f"call_{index}",
                        "type": "function",
                        "function": {"name": func.get("name", ""), "arguments": args},
                    }
                )
            out.append({"role": "assistant", "content": msg.get("content") or None, "tool_calls": calls})
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id") or "call_0",
                    "content": msg.get("content", ""),
                }
            )
        else:
            entry: Dict[str, Any] = {"role": role, "content": msg.get("content", "")}
            # الصور: الصيغة الداخلية تستخدم images=[base64]، وOpenAI يستخدم content متعدد الأجزاء
            if msg.get("images"):
                parts: List[Dict[str, Any]] = [{"type": "text", "text": msg.get("content", "")}]
                for encoded in msg["images"]:
                    parts.append(
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
                    )
                entry["content"] = parts
            out.append(entry)
    return out


class OpenAICompatClient:
    """عميل نصي متوافق مع OpenAI Chat Completions."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        temperature: float = 0.1,
        timeout: int = 180,
        max_tokens: int = 4096,
    ):
        # غياب مفتاح API لا يمنع الخادم من الإقلاع.
        # ensure_ready() سيبلغ عن عدم جاهزية النموذج فقط عند محاولة استخدامه.
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens

    # ------------------------------------------------------------ فحوصات
    def is_alive(self) -> bool:
        if not self.api_key:
            return False
        try:
            resp = requests.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10,
            )
            return resp.status_code < 500
        except requests.RequestException:
            return False

    def list_models(self) -> List[str]:
        try:
            resp = requests.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=15,
            )
            resp.raise_for_status()
            return [m.get("id", "") for m in resp.json().get("data", [])]
        except requests.RequestException as exc:
            raise LLMError(f"تعذّر الوصول إلى {self.base_url}: {exc}") from exc

    def ensure_ready(self) -> None:
        if not self.api_key:
            raise LLMError(
                "نموذج OpenAI غير متصل حالياً: لم يتم ضبط CODEAGENT_LLM_API_KEY. "
                "الخادم والواجهة يعملان، لكن ميزات الذكاء الاصطناعي ستبقى غير متاحة حتى إضافة المفتاح في Railway."
            )

    # ------------------------------------------------------------ المحادثة
    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            raise LLMError(
                "نموذج OpenAI غير متاح: لم يتم ضبط CODEAGENT_LLM_API_KEY. "
                "أضف المفتاح في Railway ثم أعد المحاولة."
            )
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise LLMError(f"انتهت مهلة انتظار المزوّد ({self.timeout}s).") from exc
        except requests.RequestException as exc:
            raise LLMError(f"فشل الاتصال بالمزوّد: {exc}") from exc

        if resp.status_code == 401:
            raise LLMError("مفتاح المزوّد مرفوض (401). تحقق من CODEAGENT_LLM_API_KEY.")
        if resp.status_code == 429:
            raise LLMError("تجاوزت حصة المزوّد أو حد الطلبات (429). انتظر قليلاً أو راجع رصيدك.")
        if resp.status_code >= 400:
            detail = resp.text[:400]
            if "tool" in detail.lower() and resp.status_code == 400:
                raise LLMError(
                    f"النموذج '{self.model}' قد لا يدعم استدعاء الأدوات. "
                    "جرّب gpt-4o-mini أو llama-3.3-70b-versatile.\n" + detail
                )
            raise LLMError(f"خطأ من المزوّد ({resp.status_code}): {detail}")

        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError("استجابة المزوّد ليست JSON صالحاً.") from exc

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": _as_openai_messages(messages),
            "max_completion_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        data = self._post(payload)
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("المزوّد لم يُرجع أي استجابة.")
        message = choices[0].get("message", {}) or {}

        calls: List[ToolCall] = []
        for item in message.get("tool_calls") or []:
            func = item.get("function", {}) or {}
            raw_args = func.get("arguments", "{}")
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    raw_args = {"_raw": raw_args}
            if not isinstance(raw_args, dict):
                raw_args = {"_raw": raw_args}
            if func.get("name"):
                calls.append(ToolCall(name=func["name"], arguments=raw_args, id=item.get("id", "")))

        return LLMResponse(content=message.get("content") or "", tool_calls=calls, raw=data)


class OpenAIVisionClient(OpenAICompatClient):
    """نفس العميل مضبوطاً لمهام الرؤية، بواجهة VisionClient نفسها."""

    def __init__(self, *args: Any, max_dimension: int = 1600, strip_exif: bool = True, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.max_dimension = max_dimension
        self.strip_exif = strip_exif

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
            parts.append(f"\nسياق المشروع (للاتساق مع الموجود):\n{project_context[:3000]}")

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
