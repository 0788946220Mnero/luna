"""نظام المهارات: ملفات إرشادات يقرؤها الوكيل قبل العمل.

الفكرة: النموذج يعرف البرمجة عموماً، لكنه لا يعرف *كيف تُبنى* مخرجات جيدة
في سياق محدد. المهارة ملف Markdown فيه المعرفة الإجرائية: الأنماط، الأخطاء
الشائعة، المكتبات المتاحة، وقائمة تحقق نهائية.

مصدران:
  1. مدمجة — داخل الحزمة، تُشحن مع المشروع
  2. مخصصة — مجلد skills/ داخل مساحة عمل المستخدم، يضيف فيها أسلوب بيته
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

BUILTIN_DIR = Path(__file__).resolve().parent / "skills_data"
USER_DIRNAME = "skills"
MAX_SKILL_BYTES = 60_000


@dataclass
class Skill:
    name: str
    description: str
    triggers: List[str] = field(default_factory=list)
    body: str = ""
    source: str = "builtin"       # builtin | user
    path: Optional[Path] = None

    def summary_line(self) -> str:
        return f"- {self.name}: {self.description}"

    def public(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "triggers": self.triggers,
            "source": self.source,
            "size": len(self.body),
        }


_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_skill(text: str, fallback_name: str, source: str = "builtin",
                path: Optional[Path] = None) -> Optional[Skill]:
    """يقرأ ملف مهارة بصيغة frontmatter بسيطة."""
    match = _FRONT_RE.match(text)
    meta: Dict[str, str] = {}
    body = text

    if match:
        body = text[match.end():]
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip().strip('"').strip("'")

    name = meta.get("name") or fallback_name
    description = meta.get("description") or ""
    if not description:
        first = next((l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")), "")
        description = first[:160]

    triggers = [t.strip() for t in re.split(r"[,،]", meta.get("triggers", "")) if t.strip()]
    if not name.strip():
        return None
    return Skill(name=name.strip(), description=description, triggers=triggers,
                 body=body.strip(), source=source, path=path)


def _load_dir(directory: Path, source: str) -> List[Skill]:
    skills: List[Skill] = []
    if not directory.is_dir():
        return skills
    for file in sorted(directory.glob("*.md")):
        try:
            if file.stat().st_size > MAX_SKILL_BYTES:
                continue
            skill = parse_skill(file.read_text(encoding="utf-8", errors="replace"),
                                file.stem, source=source, path=file)
        except OSError:
            continue
        if skill:
            skills.append(skill)
    return skills


class SkillRegistry:
    """سجل المهارات: المدمجة أولاً، ومهارات المستخدم تتقدّم عليها عند تطابق الاسم."""

    def __init__(self, user_dir: Optional[Path] = None):
        self.user_dir = user_dir
        self._skills: Dict[str, Skill] = {}
        self.reload()

    def reload(self) -> None:
        self._skills = {}
        for skill in _load_dir(BUILTIN_DIR, "builtin"):
            self._skills[skill.name] = skill
        if self.user_dir:
            for skill in _load_dir(self.user_dir, "user"):
                self._skills[skill.name] = skill      # المخصصة تتقدّم

    def all(self) -> List[Skill]:
        return sorted(self._skills.values(), key=lambda s: (s.source != "user", s.name))

    def get(self, name: str) -> Optional[Skill]:
        key = (name or "").strip()
        if key in self._skills:
            return self._skills[key]
        low = key.lower()
        for skill_name, skill in self._skills.items():
            if skill_name.lower() == low:
                return skill
        return None

    def match(self, text: str, limit: int = 3) -> List[Skill]:
        """يرشّح المهارات المناسبة لنص المهمة بمطابقة الكلمات المفتاحية."""
        haystack = (text or "").lower()
        scored: List[tuple] = []
        for skill in self._skills.values():
            score = 0
            for trigger in skill.triggers:
                if trigger.lower() in haystack:
                    score += 2
            if skill.name.lower() in haystack:
                score += 3
            if score:
                scored.append((score, skill))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:limit]]

    def catalog(self) -> str:
        """قائمة مختصرة تُحقن في توجيه النظام."""
        skills = self.all()
        if not skills:
            return ""
        lines = [s.summary_line() for s in skills]
        return (
            "المهارات المتاحة — اقرأ المهارة المناسبة بـ read_skill قبل أن تبدأ العمل:\n"
            + "\n".join(lines)
        )


_registry_cache: Dict[str, SkillRegistry] = {}


def get_registry(root: Optional[Path] = None) -> SkillRegistry:
    """سجل لكل مساحة عمل، مع كاش لتفادي إعادة القراءة في كل طلب."""
    key = str(root) if root else "_global"
    if key not in _registry_cache:
        user_dir = (root / USER_DIRNAME) if root else None
        _registry_cache[key] = SkillRegistry(user_dir)
    return _registry_cache[key]


def clear_cache() -> None:
    _registry_cache.clear()
