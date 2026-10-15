"""SQLite 访问层：绑定、token、意图、草稿、指派水位、事件去重。"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
  github_login   TEXT PRIMARY KEY,
  feishu_open_id TEXT NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_tokens (
  github_login TEXT PRIMARY KEY,
  access_enc   BLOB NOT NULL,
  refresh_enc  BLOB,
  scopes       TEXT,
  expires_at   TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intents (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  repo       TEXT NOT NULL,
  pr_number  INTEGER NOT NULL,
  requester  TEXT NOT NULL,
  body       TEXT NOT NULL,
  status     TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id  INTEGER NOT NULL,
  repo       TEXT NOT NULL,
  pr_number  INTEGER NOT NULL,
  reviewer   TEXT NOT NULL,
  findings   TEXT NOT NULL,      -- JSON：{summary, findings:[...]}
  status     TEXT NOT NULL,      -- draft / pending_confirm / published / rejected
  review_id  INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
  repo       TEXT NOT NULL,
  pr_number  INTEGER NOT NULL,
  login      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (repo, pr_number, login)
);

CREATE TABLE IF NOT EXISTS processed_events (
  event_id TEXT PRIMARY KEY,
  ts       TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(SCHEMA)

    # ---------- 事件去重 ----------
    def mark_event(self, event_id: str) -> bool:
        """返回 True 表示首次见到该事件（可继续处理）；False 表示重复。

        event_id 为空时不参与去重，视为首次。
        """
        if not event_id:
            return True
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_events (event_id, ts) VALUES (?, ?)",
                (event_id, _now()),
            )
            return cur.rowcount == 1

    # ---------- bindings ----------
    def upsert_binding(self, github_login: str, open_id: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO bindings (github_login, feishu_open_id, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(github_login) DO UPDATE SET feishu_open_id=excluded.feishu_open_id""",
                (github_login.lower(), open_id, _now()),
            )

    def get_binding(self, github_login: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM bindings WHERE github_login=?", (github_login.lower(),)
            ).fetchone()

    def get_binding_by_open_id(self, open_id: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM bindings WHERE feishu_open_id=?", (open_id,)
            ).fetchone()

    # ---------- user_tokens（Fernet 密文）----------
    def upsert_user_token(
        self,
        github_login: str,
        access_enc: bytes,
        refresh_enc: Optional[bytes],
        expires_at: Optional[str],
        scopes: Optional[str],
    ) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO user_tokens (github_login, access_enc, refresh_enc, scopes, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(github_login) DO UPDATE SET
                     access_enc=excluded.access_enc,
                     refresh_enc=excluded.refresh_enc,
                     scopes=excluded.scopes,
                     expires_at=excluded.expires_at""",
                (github_login.lower(), access_enc, refresh_enc, scopes, expires_at, _now()),
            )

    def get_user_token(self, github_login: str) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM user_tokens WHERE github_login=?", (github_login.lower(),)
            ).fetchone()

    def authorized_logins(self) -> list[str]:
        """已授权（有 gho_ token）的检视人 login 列表。"""
        with self._conn() as conn:
            rows = conn.execute("SELECT github_login FROM user_tokens").fetchall()
        return [r["github_login"] for r in rows]

    # ---------- intents ----------
    def create_intent(self, repo: str, pr_number: int, requester: str, body: str) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO intents (repo, pr_number, requester, body, status, created_at)
                   VALUES (?, ?, ?, ?, 'open', ?)""",
                (repo, pr_number, requester.lower(), body, _now()),
            )
            return int(cur.lastrowid)

    def get_intent(self, intent_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()

    def set_intent_status(self, intent_id: int, status: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("UPDATE intents SET status=? WHERE id=?", (status, intent_id))

    # ---------- drafts ----------
    def create_draft(
        self,
        intent_id: int,
        repo: str,
        pr_number: int,
        reviewer: str,
        findings: dict,
        status: str = "pending_confirm",
    ) -> int:
        """findings 存整个 LLM 结果 dict：{summary, findings:[...]}。"""
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO drafts (intent_id, repo, pr_number, reviewer, findings, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (intent_id, repo, pr_number, reviewer.lower(),
                 json.dumps(findings, ensure_ascii=False), status, _now(), _now()),
            )
            return int(cur.lastrowid)

    def get_draft(self, draft_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM drafts WHERE id=?", (int(draft_id),)).fetchone()

    def set_draft_status(self, draft_id: int, status: str, comment_id: Optional[int] = None) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE drafts SET status=?, review_id=COALESCE(?, review_id), updated_at=? WHERE id=?",
                (status, comment_id, _now(), draft_id),
            )

    # ---------- assignments（轮询水位，按「repo+PR+检视人」记录）----------
    def current_assignments(self) -> set[tuple[str, int, str]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT repo, pr_number, login FROM assignments").fetchall()
        return {(r["repo"], r["pr_number"], r["login"]) for r in rows}

    def add_assignment(self, repo: str, pr_number: int, login: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO assignments (repo, pr_number, login, created_at)
                   VALUES (?, ?, ?, ?)""",
                (repo, pr_number, login.lower(), _now()),
            )

    def remove_assignment(self, repo: str, pr_number: int, login: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "DELETE FROM assignments WHERE repo=? AND pr_number=? AND login=?",
                (repo, pr_number, login.lower()),
            )
