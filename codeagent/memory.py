"""ذاكرة المحادثات: MongoDB Atlas مع بديل SQLite محلي.

التصميم: واجهة واحدة (MemoryStore) بتنفيذين. الإنتاج على Atlas،
والاختبار والتشغيل المحلي على SQLite بلا أي تغيير في بقية الكود.

ثلاث طبقات ذاكرة:
  1. المحادثات والرسائل — محفوظة ومربوطة بالحساب
  2. ملخّص تدريجي — يضغط الأقدم فيبقى السياق ثابت التكلفة
  3. حقائق دائمة — تنتقل بين كل محادثات المستخدم
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_TITLE = 80
MAX_CONTENT = 200_000
MAX_FACT = 400
MAX_FACTS = 60


def now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


def make_title(text: str) -> str:
    clean = " ".join((text or "").split())[:MAX_TITLE]
    return clean or "محادثة جديدة"


@dataclass
class Message:
    id: str
    conversation_id: str
    role: str
    content: str
    attachments: List[str] = field(default_factory=list)
    tool_names: List[str] = field(default_factory=list)
    created_at: float = 0.0

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "attachments": self.attachments,
            "tool_names": self.tool_names,
            "created_at": self.created_at,
        }


@dataclass
class Conversation:
    id: str
    user_id: int
    title: str
    created_at: float
    updated_at: float
    summary: str = ""
    message_count: int = 0
    summarized_upto: int = 0     # عدد الرسائل المطوية في الملخّص

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "has_summary": bool(self.summary),
        }


class MemoryStore(ABC):
    """الواجهة التي تعتمد عليها بقية الخدمة."""

    backend = "abstract"

    @abstractmethod
    async def ping(self) -> bool: ...

    @abstractmethod
    async def create_conversation(self, user_id: int, title: str = "") -> Conversation: ...

    @abstractmethod
    async def get_conversation(self, conversation_id: str, user_id: int) -> Optional[Conversation]: ...

    @abstractmethod
    async def list_conversations(self, user_id: int, limit: int = 50) -> List[Conversation]: ...

    @abstractmethod
    async def rename_conversation(self, conversation_id: str, user_id: int, title: str) -> bool: ...

    @abstractmethod
    async def delete_conversation(self, conversation_id: str, user_id: int) -> bool: ...

    @abstractmethod
    async def add_message(self, conversation_id: str, user_id: int, role: str, content: str,
                          attachments: Optional[List[str]] = None,
                          tool_names: Optional[List[str]] = None) -> Message: ...

    @abstractmethod
    async def get_messages(self, conversation_id: str, user_id: int,
                           limit: int = 200, skip: int = 0) -> List[Message]: ...

    @abstractmethod
    async def set_summary(self, conversation_id: str, user_id: int,
                          summary: str, upto: int) -> None: ...

    @abstractmethod
    async def get_facts(self, user_id: int) -> List[Dict[str, Any]]: ...

    @abstractmethod
    async def add_fact(self, user_id: int, text: str, source: str = "manual") -> Dict[str, Any]: ...

    @abstractmethod
    async def delete_fact(self, user_id: int, fact_id: str) -> bool: ...

    @abstractmethod
    async def stats(self) -> Dict[str, Any]: ...

    async def close(self) -> None:
        return None

    # ---------------------------------------------------- بناء السياق
    async def build_context(
        self,
        conversation_id: str,
        user_id: int,
        keep_recent: int = 16,
    ) -> Dict[str, Any]:
        """يجمع الحقائق الدائمة + الملخّص + آخر الرسائل في سياق جاهز للنموذج."""
        conv = await self.get_conversation(conversation_id, user_id)
        if conv is None:
            return {"system": "", "messages": [], "conversation": None}

        facts = await self.get_facts(user_id)
        blocks: List[str] = []
        if facts:
            lines = "\n".join(f"- {f['text']}" for f in facts[:MAX_FACTS])
            blocks.append(f"ما تعرفه عن هذا المستخدم من محادثات سابقة:\n{lines}")
        if conv.summary:
            blocks.append(f"ملخّص ما سبق في هذه المحادثة:\n{conv.summary}")

        recent = await self.get_messages(
            conversation_id, user_id, limit=keep_recent, skip=max(0, conv.message_count - keep_recent)
        )
        return {
            "system": "\n\n".join(blocks),
            "messages": [{"role": m.role, "content": m.content} for m in recent],
            "conversation": conv,
        }

    def needs_summary(self, conv: Conversation, keep_recent: int, trigger: int) -> bool:
        unsummarized = conv.message_count - conv.summarized_upto
        return unsummarized > (keep_recent + trigger)


# ══════════════════════════════════════════════ MongoDB Atlas

class MongoMemory(MemoryStore):
    """التنفيذ على MongoDB Atlas عبر motor (غير متزامن)."""

    backend = "mongodb"

    def __init__(self, uri: str, db_name: str = "codeagent", timeout_ms: int = 8000):
        from motor.motor_asyncio import AsyncIOMotorClient

        self.uri = uri
        self.client = AsyncIOMotorClient(
            uri,
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
            retryWrites=True,
            appname="codeagent",
        )
        self.db = self.client[db_name]
        self.conversations = self.db["conversations"]
        self.messages = self.db["messages"]
        self.facts = self.db["user_facts"]
        self._indexed = False

    async def ensure_indexes(self) -> None:
        if self._indexed:
            return
        await self.conversations.create_index([("user_id", 1), ("updated_at", -1)])
        await self.conversations.create_index("id", unique=True)
        await self.messages.create_index([("conversation_id", 1), ("created_at", 1)])
        await self.messages.create_index([("user_id", 1), ("created_at", -1)])
        await self.facts.create_index([("user_id", 1), ("created_at", -1)])
        self._indexed = True

    async def ping(self) -> bool:
        try:
            await self.client.admin.command("ping")
            await self.ensure_indexes()
            return True
        except Exception:  # noqa: BLE001 — أي فشل اتصال يعني غير جاهز
            return False

    # -------------------------------------------------- المحادثات
    async def create_conversation(self, user_id: int, title: str = "") -> Conversation:
        conv = Conversation(
            id=new_id(), user_id=user_id, title=make_title(title),
            created_at=now(), updated_at=now(),
        )
        await self.conversations.insert_one({
            "id": conv.id, "user_id": user_id, "title": conv.title,
            "created_at": conv.created_at, "updated_at": conv.updated_at,
            "summary": "", "message_count": 0, "summarized_upto": 0,
        })
        return conv

    @staticmethod
    def _to_conv(doc: Dict[str, Any]) -> Conversation:
        return Conversation(
            id=doc["id"], user_id=doc["user_id"], title=doc.get("title", ""),
            created_at=doc.get("created_at", 0), updated_at=doc.get("updated_at", 0),
            summary=doc.get("summary", ""), message_count=doc.get("message_count", 0),
            summarized_upto=doc.get("summarized_upto", 0),
        )

    async def get_conversation(self, conversation_id: str, user_id: int) -> Optional[Conversation]:
        doc = await self.conversations.find_one({"id": conversation_id, "user_id": user_id})
        return self._to_conv(doc) if doc else None

    async def list_conversations(self, user_id: int, limit: int = 50) -> List[Conversation]:
        cursor = self.conversations.find({"user_id": user_id}).sort("updated_at", -1).limit(limit)
        return [self._to_conv(d) async for d in cursor]

    async def rename_conversation(self, conversation_id: str, user_id: int, title: str) -> bool:
        res = await self.conversations.update_one(
            {"id": conversation_id, "user_id": user_id},
            {"$set": {"title": make_title(title), "updated_at": now()}},
        )
        return res.matched_count > 0

    async def delete_conversation(self, conversation_id: str, user_id: int) -> bool:
        res = await self.conversations.delete_one({"id": conversation_id, "user_id": user_id})
        if res.deleted_count:
            await self.messages.delete_many({"conversation_id": conversation_id})
            return True
        return False

    # -------------------------------------------------- الرسائل
    async def add_message(self, conversation_id: str, user_id: int, role: str, content: str,
                          attachments: Optional[List[str]] = None,
                          tool_names: Optional[List[str]] = None) -> Message:
        msg = Message(
            id=new_id(), conversation_id=conversation_id, role=role,
            content=(content or "")[:MAX_CONTENT], attachments=attachments or [],
            tool_names=tool_names or [], created_at=now(),
        )
        await self.messages.insert_one({
            "id": msg.id, "conversation_id": conversation_id, "user_id": user_id,
            "role": role, "content": msg.content, "attachments": msg.attachments,
            "tool_names": msg.tool_names, "created_at": msg.created_at,
        })
        update: Dict[str, Any] = {"$inc": {"message_count": 1}, "$set": {"updated_at": now()}}
        conv = await self.conversations.find_one({"id": conversation_id, "user_id": user_id})
        if conv and role == "user" and conv.get("message_count", 0) == 0:
            update["$set"]["title"] = make_title(content)
        await self.conversations.update_one({"id": conversation_id, "user_id": user_id}, update)
        return msg

    async def get_messages(self, conversation_id: str, user_id: int,
                           limit: int = 200, skip: int = 0) -> List[Message]:
        cursor = (self.messages.find({"conversation_id": conversation_id, "user_id": user_id})
                  .sort("created_at", 1).skip(max(0, skip)).limit(limit))
        return [
            Message(
                id=d["id"], conversation_id=conversation_id, role=d["role"],
                content=d.get("content", ""), attachments=d.get("attachments", []),
                tool_names=d.get("tool_names", []), created_at=d.get("created_at", 0),
            )
            async for d in cursor
        ]

    async def set_summary(self, conversation_id: str, user_id: int, summary: str, upto: int) -> None:
        await self.conversations.update_one(
            {"id": conversation_id, "user_id": user_id},
            {"$set": {"summary": summary[:20000], "summarized_upto": upto, "updated_at": now()}},
        )

    # -------------------------------------------------- الحقائق
    async def get_facts(self, user_id: int) -> List[Dict[str, Any]]:
        cursor = self.facts.find({"user_id": user_id}).sort("created_at", -1).limit(MAX_FACTS)
        return [
            {"id": d["id"], "text": d["text"], "source": d.get("source", "manual"),
             "created_at": d.get("created_at", 0)}
            async for d in cursor
        ]

    async def add_fact(self, user_id: int, text: str, source: str = "manual") -> Dict[str, Any]:
        clean = " ".join((text or "").split())[:MAX_FACT]
        if not clean:
            raise ValueError("نص الحقيقة فارغ.")
        existing = await self.facts.find_one({"user_id": user_id, "text": clean})
        if existing:
            return {"id": existing["id"], "text": clean, "source": existing.get("source", source),
                    "created_at": existing.get("created_at", 0)}
        doc = {"id": new_id(), "user_id": user_id, "text": clean,
               "source": source, "created_at": now()}
        await self.facts.insert_one(dict(doc))
        count = await self.facts.count_documents({"user_id": user_id})
        if count > MAX_FACTS:
            oldest = self.facts.find({"user_id": user_id}).sort("created_at", 1).limit(count - MAX_FACTS)
            async for old in oldest:
                await self.facts.delete_one({"id": old["id"]})
        doc.pop("user_id", None)
        return doc

    async def delete_fact(self, user_id: int, fact_id: str) -> bool:
        res = await self.facts.delete_one({"user_id": user_id, "id": fact_id})
        return res.deleted_count > 0

    async def stats(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "conversations": await self.conversations.count_documents({}),
            "messages": await self.messages.count_documents({}),
            "facts": await self.facts.count_documents({}),
        }

    async def close(self) -> None:
        self.client.close()


# ══════════════════════════════════════════════ SQLite (بديل محلي)

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT,
    created_at REAL, updated_at REAL, summary TEXT DEFAULT '',
    message_count INTEGER DEFAULT 0, summarized_upto INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, user_id INTEGER NOT NULL,
    role TEXT, content TEXT, attachments TEXT, tool_names TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS user_facts (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, text TEXT,
    source TEXT, created_at REAL
);
CREATE INDEX IF NOT EXISTS ix_conv_user ON conversations(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS ix_fact_user ON user_facts(user_id, created_at DESC);
"""


class SqliteMemory(MemoryStore):
    """بديل محلي بنفس الواجهة — يعمل بلا إنترنت وبلا Atlas."""

    backend = "sqlite"

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SQLITE_SCHEMA)
        self.conn.commit()

    async def ping(self) -> bool:
        try:
            self.conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    async def create_conversation(self, user_id: int, title: str = "") -> Conversation:
        conv = Conversation(id=new_id(), user_id=user_id, title=make_title(title),
                            created_at=now(), updated_at=now())
        self.conn.execute(
            "INSERT INTO conversations (id,user_id,title,created_at,updated_at,summary,"
            "message_count,summarized_upto) VALUES (?,?,?,?,?,'',0,0)",
            (conv.id, user_id, conv.title, conv.created_at, conv.updated_at),
        )
        self.conn.commit()
        return conv

    @staticmethod
    def _to_conv(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=row["id"], user_id=row["user_id"], title=row["title"] or "",
            created_at=row["created_at"], updated_at=row["updated_at"],
            summary=row["summary"] or "", message_count=row["message_count"] or 0,
            summarized_upto=row["summarized_upto"] or 0,
        )

    async def get_conversation(self, conversation_id: str, user_id: int) -> Optional[Conversation]:
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)
        ).fetchone()
        return self._to_conv(row) if row else None

    async def list_conversations(self, user_id: int, limit: int = 50) -> List[Conversation]:
        rows = self.conn.execute(
            "SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [self._to_conv(r) for r in rows]

    async def rename_conversation(self, conversation_id: str, user_id: int, title: str) -> bool:
        cur = self.conn.execute(
            "UPDATE conversations SET title=?, updated_at=? WHERE id=? AND user_id=?",
            (make_title(title), now(), conversation_id, user_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    async def delete_conversation(self, conversation_id: str, user_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)
        )
        self.conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
        self.conn.commit()
        return cur.rowcount > 0

    async def add_message(self, conversation_id: str, user_id: int, role: str, content: str,
                          attachments: Optional[List[str]] = None,
                          tool_names: Optional[List[str]] = None) -> Message:
        msg = Message(id=new_id(), conversation_id=conversation_id, role=role,
                      content=(content or "")[:MAX_CONTENT], attachments=attachments or [],
                      tool_names=tool_names or [], created_at=now())
        self.conn.execute(
            "INSERT INTO messages (id,conversation_id,user_id,role,content,attachments,"
            "tool_names,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (msg.id, conversation_id, user_id, role, msg.content,
             json.dumps(msg.attachments, ensure_ascii=False),
             json.dumps(msg.tool_names, ensure_ascii=False), msg.created_at),
        )
        row = self.conn.execute(
            "SELECT message_count FROM conversations WHERE id=? AND user_id=?",
            (conversation_id, user_id),
        ).fetchone()
        if row is not None:
            if role == "user" and (row["message_count"] or 0) == 0:
                self.conn.execute(
                    "UPDATE conversations SET title=? WHERE id=?", (make_title(content), conversation_id)
                )
            self.conn.execute(
                "UPDATE conversations SET message_count=message_count+1, updated_at=?"
                " WHERE id=? AND user_id=?",
                (now(), conversation_id, user_id),
            )
        self.conn.commit()
        return msg

    async def get_messages(self, conversation_id: str, user_id: int,
                           limit: int = 200, skip: int = 0) -> List[Message]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? AND user_id=?"
            " ORDER BY created_at LIMIT ? OFFSET ?",
            (conversation_id, user_id, limit, max(0, skip)),
        ).fetchall()
        out = []
        for r in rows:
            try:
                atts = json.loads(r["attachments"] or "[]")
                tools = json.loads(r["tool_names"] or "[]")
            except json.JSONDecodeError:
                atts, tools = [], []
            out.append(Message(id=r["id"], conversation_id=conversation_id, role=r["role"],
                               content=r["content"] or "", attachments=atts,
                               tool_names=tools, created_at=r["created_at"]))
        return out

    async def set_summary(self, conversation_id: str, user_id: int, summary: str, upto: int) -> None:
        self.conn.execute(
            "UPDATE conversations SET summary=?, summarized_upto=?, updated_at=?"
            " WHERE id=? AND user_id=?",
            (summary[:20000], upto, now(), conversation_id, user_id),
        )
        self.conn.commit()

    async def get_facts(self, user_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM user_facts WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, MAX_FACTS),
        ).fetchall()
        return [{"id": r["id"], "text": r["text"], "source": r["source"],
                 "created_at": r["created_at"]} for r in rows]

    async def add_fact(self, user_id: int, text: str, source: str = "manual") -> Dict[str, Any]:
        clean = " ".join((text or "").split())[:MAX_FACT]
        if not clean:
            raise ValueError("نص الحقيقة فارغ.")
        row = self.conn.execute(
            "SELECT * FROM user_facts WHERE user_id=? AND text=?", (user_id, clean)
        ).fetchone()
        if row:
            return {"id": row["id"], "text": clean, "source": row["source"],
                    "created_at": row["created_at"]}
        doc = {"id": new_id(), "text": clean, "source": source, "created_at": now()}
        self.conn.execute(
            "INSERT INTO user_facts (id,user_id,text,source,created_at) VALUES (?,?,?,?,?)",
            (doc["id"], user_id, clean, source, doc["created_at"]),
        )
        self.conn.commit()
        return doc

    async def delete_fact(self, user_id: int, fact_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM user_facts WHERE user_id=? AND id=?", (user_id, fact_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    async def stats(self) -> Dict[str, Any]:
        c = self.conn.execute("SELECT COUNT(*) n FROM conversations").fetchone()["n"]
        m = self.conn.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
        f = self.conn.execute("SELECT COUNT(*) n FROM user_facts").fetchone()["n"]
        return {"backend": self.backend, "conversations": c, "messages": m, "facts": f}

    async def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


# ══════════════════════════════════════════════ التلخيص

SUMMARY_PROMPT = """لخّص هذا الجزء من محادثة بين مستخدم ومساعد برمجي.
اكتب فقرة واحدة موجزة تحفظ: ما طُلب، ما أُنجز، القرارات المتخذة، أسماء الملفات والمسارات المهمة.
لا تخترع شيئاً غير موجود. لا تكتب مقدمة ولا خاتمة — الملخّص فقط."""


def summarize_messages(client, older: List[Message], previous: str = "") -> str:
    """يضغط الرسائل القديمة في فقرة. يُستدعى عند تجاوز الحد."""
    if not older:
        return previous
    body = "\n".join(
        f"{'المستخدم' if m.role == 'user' else 'المساعد'}: {m.content[:1500]}" for m in older
    )
    prompt = body if not previous else f"الملخّص السابق:\n{previous}\n\nالجديد:\n{body}"
    response = client.chat([
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": prompt[:40000]},
    ])
    return (response.content or previous).strip()


FACTS_PROMPT = """استخرج من هذه المحادثة الحقائق الدائمة عن المستخدم فقط:
تفضيلاته التقنية، لغات ومكتبات يستخدمها، أسماء مشاريعه، أسلوب عمله، قيود يذكرها.

قواعد صارمة:
- حقائق دائمة تنفع في محادثات مستقبلية فقط. لا تفاصيل عابرة عن مهمة اليوم.
- لا تستخرج كلمات مرور ولا مفاتيح ولا معلومات شخصية حساسة.
- كل حقيقة سطر واحد قصير.
- إن لم توجد حقائق دائمة، أعد مصفوفة فارغة.

أعد JSON فقط بهذا الشكل: {"facts": ["...", "..."]}"""


def extract_facts(client, messages: List[Message], limit: int = 5) -> List[str]:
    """يستخرج حقائق دائمة من محادثة. يُستدعى دورياً لا مع كل رسالة."""
    if not messages:
        return []
    body = "\n".join(
        f"{'المستخدم' if m.role == 'user' else 'المساعد'}: {m.content[:800]}" for m in messages
    )
    try:
        response = client.chat([
            {"role": "system", "content": FACTS_PROMPT},
            {"role": "user", "content": body[:30000]},
        ])
        text = (response.content or "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return []
        data = json.loads(text[start:end + 1])
        facts = data.get("facts", [])
        return [str(f).strip()[:MAX_FACT] for f in facts if str(f).strip()][:limit]
    except Exception:  # noqa: BLE001 — فشل الاستخراج لا يعطّل المحادثة
        return []


def build_memory(uri: str, db_name: str, fallback_path: Path) -> MemoryStore:
    """ينشئ المخزن: Mongo إن وُجد URI، وإلا SQLite محلي."""
    if uri and uri.strip():
        return MongoMemory(uri.strip(), db_name or "codeagent")
    return SqliteMemory(fallback_path)
