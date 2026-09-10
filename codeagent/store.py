"""سجل العمليات والذاكرة: قاعدة بيانات SQLite بسيطة."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session     TEXT NOT NULL,
    ts          REAL NOT NULL,
    tool        TEXT NOT NULL,
    args        TEXT NOT NULL,
    status      TEXT NOT NULL,
    summary     TEXT,
    undone      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS backups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id       INTEGER NOT NULL,
    rel_path    TEXT NOT NULL,
    backup_path TEXT,
    existed     INTEGER NOT NULL,
    ts          REAL NOT NULL,
    FOREIGN KEY (op_id) REFERENCES operations(id)
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session     TEXT NOT NULL,
    ts          REAL NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ops_session ON operations(session);
CREATE INDEX IF NOT EXISTS idx_backups_op ON backups(op_id);
"""


@dataclass
class Operation:
    id: int
    session: str
    ts: float
    tool: str
    args: Dict[str, Any]
    status: str
    summary: str
    undone: bool


class Store:
    """واجهة قاعدة البيانات. آمنة للاستخدام كـ context manager."""

    def __init__(self, db_path: Path, session: Optional[str] = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.session = session or uuid.uuid4().hex[:12]
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------- العمليات
    def log_operation(
        self,
        tool: str,
        args: Dict[str, Any],
        status: str,
        summary: str = "",
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO operations (session, ts, tool, args, status, summary)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.session,
                time.time(),
                tool,
                json.dumps(args, ensure_ascii=False, default=str)[:20000],
                status,
                (summary or "")[:4000],
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_operation(self, op_id: int, status: str, summary: str = "") -> None:
        self.conn.execute(
            "UPDATE operations SET status = ?, summary = ? WHERE id = ?",
            (status, (summary or "")[:4000], op_id),
        )
        self.conn.commit()

    def mark_undone(self, op_id: int) -> None:
        self.conn.execute("UPDATE operations SET undone = 1 WHERE id = ?", (op_id,))
        self.conn.commit()

    def recent_operations(self, limit: int = 20) -> List[Operation]:
        rows = self.conn.execute(
            "SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_op(r) for r in rows]

    def last_undoable_operation(self) -> Optional[Operation]:
        row = self.conn.execute(
            "SELECT o.* FROM operations o"
            " WHERE o.undone = 0 AND EXISTS (SELECT 1 FROM backups b WHERE b.op_id = o.id)"
            " ORDER BY o.id DESC LIMIT 1"
        ).fetchone()
        return self._row_to_op(row) if row else None

    @staticmethod
    def _row_to_op(row: sqlite3.Row) -> Operation:
        try:
            args = json.loads(row["args"])
        except (json.JSONDecodeError, TypeError):
            args = {}
        return Operation(
            id=row["id"],
            session=row["session"],
            ts=row["ts"],
            tool=row["tool"],
            args=args,
            status=row["status"],
            summary=row["summary"] or "",
            undone=bool(row["undone"]),
        )

    # ------------------------------------------------------- النسخ الاحتياطية
    def add_backup(
        self, op_id: int, rel_path: str, backup_path: Optional[str], existed: bool
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO backups (op_id, rel_path, backup_path, existed, ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (op_id, rel_path, backup_path, 1 if existed else 0, time.time()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def backups_for(self, op_id: int) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM backups WHERE op_id = ? ORDER BY id", (op_id,)
        ).fetchall()

    # ------------------------------------------------------- الرسائل
    def add_message(self, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO messages (session, ts, role, content) VALUES (?, ?, ?, ?)",
            (self.session, time.time(), role, (content or "")[:200000]),
        )
        self.conn.commit()

    def session_messages(self, session: Optional[str] = None, limit: int = 50):
        rows = self.conn.execute(
            "SELECT role, content, ts FROM messages WHERE session = ?"
            " ORDER BY id DESC LIMIT ?",
            (session or self.session, limit),
        ).fetchall()
        return list(reversed(rows))

    # ------------------------------------------------------- meta
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def stats(self) -> Dict[str, int]:
        total = self.conn.execute("SELECT COUNT(*) c FROM operations").fetchone()["c"]
        failed = self.conn.execute(
            "SELECT COUNT(*) c FROM operations WHERE status = 'error'"
        ).fetchone()["c"]
        undone = self.conn.execute(
            "SELECT COUNT(*) c FROM operations WHERE undone = 1"
        ).fetchone()["c"]
        sessions = self.conn.execute(
            "SELECT COUNT(DISTINCT session) c FROM operations"
        ).fetchone()["c"]
        return {"operations": total, "errors": failed, "undone": undone, "sessions": sessions}

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
