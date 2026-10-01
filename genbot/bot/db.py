"""Хранилище на SQLite: пользователи, кредиты, платежи, генерации."""
from __future__ import annotations

import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    credits INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    stars INTEGER NOT NULL,
    credits INTEGER NOT NULL,
    refunded INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    prompt TEXT NOT NULL,
    cost INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


class InsufficientCredits(Exception):
    pass


class Database:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def ensure_user(self, user_id: int, username: str | None, free_credits: int) -> bool:
        """Создаёт пользователя с бесплатными кредитами. True, если он новый."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO users (id, username, credits, created_at) VALUES (?, ?, ?, ?)",
                (user_id, username, free_credits, int(time.time())),
            )
            return cur.rowcount == 1

    def balance(self, user_id: int) -> int:
        row = self._conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
        return row[0] if row else 0

    def charge(self, user_id: int, kind: str, prompt: str, cost: int) -> int:
        """Списывает кредиты и создаёт запись генерации. Возвращает её id."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "UPDATE users SET credits = credits - ? WHERE id = ? AND credits >= ?",
                    (cost, user_id, cost),
                )
                if cur.rowcount != 1:
                    raise InsufficientCredits
                gen = self._conn.execute(
                    "INSERT INTO generations (user_id, kind, prompt, cost, status, created_at)"
                    " VALUES (?, ?, ?, ?, 'pending', ?)",
                    (user_id, kind, prompt, cost, int(time.time())),
                )
                self._conn.execute("COMMIT")
                return gen.lastrowid
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def finish(self, gen_id: int, ok: bool) -> None:
        """Закрывает генерацию; при ошибке возвращает кредиты пользователю."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT user_id, cost, status FROM generations WHERE id = ?", (gen_id,)
            ).fetchone()
            if row is None or row[2] != "pending":
                self._conn.execute("ROLLBACK")
                return
            user_id, cost, _ = row
            self._conn.execute(
                "UPDATE generations SET status = ? WHERE id = ?", ("done" if ok else "failed", gen_id)
            )
            if not ok:
                self._conn.execute(
                    "UPDATE users SET credits = credits + ? WHERE id = ?", (cost, user_id)
                )
            self._conn.execute("COMMIT")

    def add_payment(self, charge_id: str, user_id: int, stars: int, credits: int) -> bool:
        """Зачисляет оплату. Повторный charge_id игнорируется. True, если зачислено."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO payments (charge_id, user_id, stars, credits, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (charge_id, user_id, stars, credits, int(time.time())),
            )
            if cur.rowcount != 1:
                self._conn.execute("ROLLBACK")
                return False
            self._conn.execute(
                "UPDATE users SET credits = credits + ? WHERE id = ?", (credits, user_id)
            )
            self._conn.execute("COMMIT")
            return True

    def get_payment(self, charge_id: str) -> tuple[int, int, int, int] | None:
        """(user_id, stars, credits, refunded) или None."""
        return self._conn.execute(
            "SELECT user_id, stars, credits, refunded FROM payments WHERE charge_id = ?",
            (charge_id,),
        ).fetchone()

    def mark_refunded(self, charge_id: str) -> None:
        """Помечает платёж возвращённым и снимает его кредиты (не ниже нуля)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT user_id, credits, refunded FROM payments WHERE charge_id = ?", (charge_id,)
            ).fetchone()
            if row is None or row[2]:
                self._conn.execute("ROLLBACK")
                return
            self._conn.execute("UPDATE payments SET refunded = 1 WHERE charge_id = ?", (charge_id,))
            self._conn.execute(
                "UPDATE users SET credits = MAX(credits - ?, 0) WHERE id = ?", (row[1], row[0])
            )
            self._conn.execute("COMMIT")

    def stats(self) -> dict[str, int]:
        q = self._conn.execute
        return {
            "users": q("SELECT COUNT(*) FROM users").fetchone()[0],
            "payers": q("SELECT COUNT(DISTINCT user_id) FROM payments WHERE refunded = 0").fetchone()[0],
            "stars": q("SELECT COALESCE(SUM(stars), 0) FROM payments WHERE refunded = 0").fetchone()[0],
            "images": q("SELECT COUNT(*) FROM generations WHERE kind = 'image' AND status = 'done'").fetchone()[0],
            "videos": q("SELECT COUNT(*) FROM generations WHERE kind = 'video' AND status = 'done'").fetchone()[0],
            "failed": q("SELECT COUNT(*) FROM generations WHERE status = 'failed'").fetchone()[0],
        }
