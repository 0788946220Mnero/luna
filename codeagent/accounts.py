"""نظام الحسابات: مستخدمون، جلسات، صلاحيات، وحصص استخدام.

التجزئة بـ scrypt من المكتبة القياسية — لا تبعيات خارجية.
توكن الجلسة يُخزَّن مجزّأً؛ تسريب قاعدة البيانات لا يمنح جلسات صالحة.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
TOKEN_BYTES = 32

MAX_FAILED = 8
LOCK_SECONDS = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT,
    pw_salt      TEXT NOT NULL,
    pw_hash      TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'user',
    active       INTEGER NOT NULL DEFAULT 1,
    daily_quota  INTEGER NOT NULL DEFAULT 100,
    can_write    INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL,
    last_login   REAL,
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    user_agent TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS usage (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, day, kind)
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_user_day ON usage(user_id, day);
"""


class AuthError(Exception):
    """فشل مصادقة أو صلاحية."""


@dataclass
class User:
    id: int
    username: str
    display_name: str
    role: str
    active: bool
    daily_quota: int          # 0 = بلا حد
    can_write: bool
    created_at: float
    last_login: Optional[float]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def unlimited(self) -> bool:
        return self.is_admin or self.daily_quota <= 0

    def public(self, used_today: int = 0) -> Dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "role": self.role,
            "active": self.active,
            "daily_quota": self.daily_quota,
            "unlimited": self.unlimited,
            "can_write": self.can_write,
            "used_today": used_today,
            "remaining": None if self.unlimited else max(0, self.daily_quota - used_today),
            "created_at": self.created_at,
            "last_login": self.last_login,
        }


def hash_password(password: str, salt: Optional[bytes] = None) -> tuple:
    salt = salt or secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return salt.hex(), derived.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    _, candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, hash_hex)


def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def validate_username(name: str) -> str:
    clean = (name or "").strip()
    if not (3 <= len(clean) <= 32):
        raise AuthError("اسم المستخدم يجب أن يكون بين 3 و32 حرفاً.")
    if not all(ch.isalnum() or ch in "._-" for ch in clean):
        raise AuthError("اسم المستخدم يقبل الحروف والأرقام و . _ - فقط.")
    return clean


def validate_password(password: str) -> str:
    if len(password or "") < 8:
        raise AuthError("كلمة المرور يجب أن تكون 8 أحرف على الأقل.")
    if len(password) > 256:
        raise AuthError("كلمة المرور طويلة جداً.")
    return password


class Accounts:
    """قاعدة الحسابات — منفصلة عن قاعدة العمليات."""

    def __init__(self, db_path: Path, session_days: int = 7):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_seconds = max(1, session_days) * 86400
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------ المستخدمون
    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"] or "",
            role=row["role"],
            active=bool(row["active"]),
            daily_quota=row["daily_quota"],
            can_write=bool(row["can_write"]),
            created_at=row["created_at"],
            last_login=row["last_login"],
        )

    def count_users(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

    def get_user(self, username: str) -> Optional[User]:
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        return self._row_to_user(row) if row else None

    def get_user_by_id(self, user_id: int) -> Optional[User]:
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        out = []
        for row in rows:
            user = self._row_to_user(row)
            out.append(user.public(self.used_today(user.id)))
        return out

    def create_user(
        self,
        username: str,
        password: str,
        role: str = "user",
        display_name: str = "",
        daily_quota: int = 100,
        can_write: bool = True,
    ) -> User:
        username = validate_username(username)
        validate_password(password)
        if role not in ("admin", "user"):
            raise AuthError("الدور يجب أن يكون admin أو user.")
        if self.get_user(username):
            raise AuthError(f"اسم المستخدم '{username}' مستخدم بالفعل.")

        salt, digest = hash_password(password)
        cur = self.conn.execute(
            "INSERT INTO users (username, display_name, pw_salt, pw_hash, role, active,"
            " daily_quota, can_write, created_at) VALUES (?,?,?,?,?,1,?,?,?)",
            (username, display_name or username, salt, digest, role,
             0 if role == "admin" else max(0, daily_quota), 1 if can_write else 0, time.time()),
        )
        self.conn.commit()
        user = self.get_user_by_id(int(cur.lastrowid))
        assert user is not None
        return user

    def update_user(self, user_id: int, **fields: Any) -> User:
        user = self.get_user_by_id(user_id)
        if not user:
            raise AuthError("المستخدم غير موجود.")

        allowed = {"display_name", "role", "active", "daily_quota", "can_write"}
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key == "role":
                if value not in ("admin", "user"):
                    raise AuthError("الدور يجب أن يكون admin أو user.")
            if key in ("active", "can_write"):
                value = 1 if value else 0
            if key == "daily_quota":
                value = max(0, int(value))
            sets.append(f"{key} = ?")
            values.append(value)

        if sets:
            values.append(user_id)
            self.conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", values)
            self.conn.commit()

        # إيقاف الحساب يُنهي جلساته فوراً
        if fields.get("active") is False:
            self.revoke_all(user_id)

        updated = self.get_user_by_id(user_id)
        assert updated is not None
        return updated

    def set_password(self, user_id: int, password: str) -> None:
        validate_password(password)
        salt, digest = hash_password(password)
        self.conn.execute(
            "UPDATE users SET pw_salt = ?, pw_hash = ?, failed_count = 0, locked_until = NULL"
            " WHERE id = ?",
            (salt, digest, user_id),
        )
        self.conn.commit()
        self.revoke_all(user_id)

    def delete_user(self, user_id: int) -> None:
        user = self.get_user_by_id(user_id)
        if not user:
            raise AuthError("المستخدم غير موجود.")
        if user.is_admin:
            admins = self.conn.execute(
                "SELECT COUNT(*) c FROM users WHERE role = 'admin' AND active = 1"
            ).fetchone()["c"]
            if admins <= 1:
                raise AuthError("لا يمكن حذف آخر حساب إدارة.")
        self.revoke_all(user_id)
        self.conn.execute("DELETE FROM usage WHERE user_id = ?", (user_id,))
        self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.conn.commit()

    def ensure_admin(self, username: str, password: str) -> Optional[User]:
        """ينشئ حساب الإدارة الأول عند الإقلاع إن لم يوجد."""
        if not username or not password:
            return None
        existing = self.get_user(username)
        if existing:
            return existing
        return self.create_user(username, password, role="admin", display_name="الإدارة")

    # ------------------------------------------------------ الجلسات
    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def login(self, username: str, password: str, user_agent: str = "") -> tuple:
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username or "",)
        ).fetchone()

        if not row:
            # تجزئة وهمية لتقريب زمن الرد ومنع كشف الأسماء الموجودة
            hash_password(password or "x")
            raise AuthError("اسم المستخدم أو كلمة المرور غير صحيحة.")

        now = time.time()
        if row["locked_until"] and row["locked_until"] > now:
            wait = int((row["locked_until"] - now) / 60) + 1
            raise AuthError(f"الحساب مقفل مؤقتاً. حاول بعد {wait} دقيقة.")

        if not row["active"]:
            raise AuthError("هذا الحساب موقوف. راجع الإدارة.")

        if not verify_password(password or "", row["pw_salt"], row["pw_hash"]):
            failed = row["failed_count"] + 1
            locked = now + LOCK_SECONDS if failed >= MAX_FAILED else None
            self.conn.execute(
                "UPDATE users SET failed_count = ?, locked_until = ? WHERE id = ?",
                (failed, locked, row["id"]),
            )
            self.conn.commit()
            if locked:
                raise AuthError("تجاوزت عدد المحاولات. الحساب مقفل 15 دقيقة.")
            raise AuthError("اسم المستخدم أو كلمة المرور غير صحيحة.")

        token = secrets.token_urlsafe(TOKEN_BYTES)
        self.conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, user_agent)"
            " VALUES (?,?,?,?,?)",
            (self._hash_token(token), row["id"], now, now + self.session_seconds, (user_agent or "")[:200]),
        )
        self.conn.execute(
            "UPDATE users SET last_login = ?, failed_count = 0, locked_until = NULL WHERE id = ?",
            (now, row["id"]),
        )
        self.conn.commit()
        self.purge_expired()
        return token, self._row_to_user(row)

    def resolve(self, token: str) -> User:
        """يحوّل توكن الجلسة إلى مستخدم، ويرفض الموقوف أو المنتهي."""
        if not token:
            raise AuthError("لا توجد جلسة.")
        row = self.conn.execute(
            "SELECT s.expires_at, u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = ?",
            (self._hash_token(token),),
        ).fetchone()
        if not row:
            raise AuthError("الجلسة غير صالحة. سجّل الدخول من جديد.")
        if row["expires_at"] < time.time():
            self.logout(token)
            raise AuthError("انتهت صلاحية الجلسة. سجّل الدخول من جديد.")
        if not row["active"]:
            self.revoke_all(row["id"])
            raise AuthError("هذا الحساب موقوف.")
        return self._row_to_user(row)

    def logout(self, token: str) -> None:
        self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash_token(token),))
        self.conn.commit()

    def revoke_all(self, user_id: int) -> int:
        cur = self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self.conn.commit()
        return cur.rowcount

    def purge_expired(self) -> int:
        cur = self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        self.conn.commit()
        return cur.rowcount

    def active_sessions(self, user_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM sessions WHERE user_id = ? AND expires_at > ?",
            (user_id, time.time()),
        ).fetchone()["c"]

    # ------------------------------------------------------ الحصص
    def used_today(self, user_id: int, kind: str = "model") -> int:
        row = self.conn.execute(
            "SELECT count FROM usage WHERE user_id = ? AND day = ? AND kind = ?",
            (user_id, today_key(), kind),
        ).fetchone()
        return row["count"] if row else 0

    def record_usage(self, user_id: int, kind: str = "model", amount: int = 1) -> int:
        self.conn.execute(
            "INSERT INTO usage (user_id, day, kind, count) VALUES (?,?,?,?)"
            " ON CONFLICT(user_id, day, kind) DO UPDATE SET count = count + excluded.count",
            (user_id, today_key(), kind, amount),
        )
        self.conn.commit()
        return self.used_today(user_id, kind)

    def check_quota(self, user: User, kind: str = "model") -> None:
        if user.unlimited:
            return
        used = self.used_today(user.id, kind)
        if used >= user.daily_quota:
            raise AuthError(
                f"استنفدت حصتك اليومية ({user.daily_quota} طلب). تتجدد غداً أو راجع الإدارة."
            )

    def usage_summary(self, days: int = 7) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT u.username, us.day, SUM(us.count) total FROM usage us"
            " JOIN users u ON u.id = us.user_id GROUP BY us.user_id, us.day"
            " ORDER BY us.day DESC LIMIT ?",
            (days * 50,),
        ).fetchall()
        return [{"username": r["username"], "day": r["day"], "total": r["total"]} for r in rows]

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
