"""حلقة الوكيل (ReAct): يفكّر، يستدعي الأدوات، يقرأ النتائج، ثم يجيب."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .executor import Executor
from .llm import LLMError, build_client
from .prompts import build_system_prompt, project_context_message
from .skills import get_registry
from .store import Store
from .tools import tool_schemas

MAX_TOOL_OUTPUT = 12_000


@dataclass
class AgentStep:
    kind: str            # thought | tool | answer | error
    name: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    text: str = ""
    ok: bool = True


@dataclass
class AgentRun:
    answer: str = ""
    steps: List[AgentStep] = field(default_factory=list)
    stopped_reason: str = "completed"


class Agent:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        executor: Executor,
        client=None,
        on_step: Optional[Callable[[AgentStep], None]] = None,
    ):
        self.cfg = cfg
        self.store = store
        self.executor = executor
        self.client = client or build_client(cfg.llm)
        self.on_step = on_step or (lambda step: None)

    def _emit(self, step: AgentStep, run: AgentRun) -> None:
        run.steps.append(step)
        self.on_step(step)

    def run(
        self,
        question: str,
        project_context: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> AgentRun:
        run = AgentRun()
        registry = get_registry(self.cfg.root)
        system = build_system_prompt(
            registry.catalog(),
            strict_validation=bool(self.cfg.data.get("validation", {}).get("strict", False)),
        )
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]

        hints = registry.match(question)
        if hints:
            messages.append({
                "role": "system",
                "content": "مهارات مرشّحة لهذه المهمة: "
                           + "، ".join(s.name for s in hints)
                           + ". اقرأها بـ read_skill قبل أن تكتب.",
            })
        if project_context:
            messages.append({"role": "system", "content": project_context_message(project_context)})
        for item in history or []:
            messages.append({"role": item["role"], "content": item["content"]})
        messages.append({"role": "user", "content": question})

        self.store.add_message("user", question)
        max_steps = int(self.cfg.llm.get("max_steps", 12))

        for step_index in range(max_steps):
            try:
                response = self.client.chat(messages, tools=tool_schemas())
            except LLMError as exc:
                run.stopped_reason = "llm_error"
                self._emit(AgentStep(kind="error", text=str(exc), ok=False), run)
                run.answer = f"تعذّر إكمال المهمة: {exc}"
                self.store.add_message("assistant", run.answer)
                return run

            if response.content.strip():
                self._emit(AgentStep(kind="thought", text=response.content.strip()), run)

            if not response.tool_calls:
                run.answer = response.content.strip() or "(لم يُرجع النموذج إجابة نصية.)"
                self._emit(AgentStep(kind="answer", text=run.answer), run)
                self.store.add_message("assistant", run.answer)
                return run

            for index, call in enumerate(response.tool_calls):
                if not call.id:
                    call.id = f"call_{step_index}_{index}"

            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {"id": call.id, "function": {"name": call.name, "arguments": call.arguments}}
                    for call in response.tool_calls
                ],
            }
            messages.append(assistant_msg)

            for call in response.tool_calls:
                result = self.executor.run(call.name, call.arguments or {})
                output = result.output or ""
                if len(output) > MAX_TOOL_OUTPUT:
                    output = output[:MAX_TOOL_OUTPUT] + "\n... [تم اقتطاع المخرجات]"
                self._emit(
                    AgentStep(kind="tool", name=call.name, args=call.arguments or {},
                              text=output, ok=result.ok),
                    run,
                )
                messages.append(
                    {
                        "role": "tool",
                        "name": call.name,
                        "tool_call_id": call.id,
                        "content": ("" if result.ok else "فشل: ") + output,
                    }
                )

        run.stopped_reason = "max_steps"
        run.answer = (
            f"توقفت بعد {max_steps} خطوة دون الوصول لإجابة نهائية. "
            "راجع الخطوات أعلاه أو أعد صياغة الطلب بشكل أدق."
        )
        self._emit(AgentStep(kind="answer", text=run.answer, ok=False), run)
        self.store.add_message("assistant", run.answer)
        return run
