"""Хранилище на SQLite: пользователи, кредиты, платежи, генерации."""
from __future__ import annotations

import json
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
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    gen_id INTEGER NOT NULL,
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
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(users)")}
        if "settings" not in cols:
            self._conn.execute("ALTER TABLE users ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'")
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

    def grant(self, user_id: int, amount: int) -> int:
        """Ручное начисление (или списание при amount < 0, не ниже нуля). Возвращает новый баланс."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO users (id, username, credits, created_at) VALUES (?, NULL, 0, ?)",
                (user_id, int(time.time())),
            )
            self._conn.execute(
                "UPDATE users SET credits = MAX(credits + ?, 0) WHERE id = ?", (amount, user_id)
            )
        return self.balance(user_id)

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

    def finish(self, gen_id: int, ok: bool, actual_cost: int | None = None) -> int:
        """Закрывает генерацию и возвращает итоговую цену.

        При ошибке кредиты возвращаются полностью. Если фактическая цена отличается от
        зарезервированной, разница возвращается или доплачивается (не больше остатка на балансе).
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT user_id, cost, status FROM generations WHERE id = ?", (gen_id,)
            ).fetchone()
            if row is None or row[2] != "pending":
                self._conn.execute("ROLLBACK")
                return row[1] if row else 0
            user_id, cost, _ = row
            final = 0 if not ok else (cost if actual_cost is None else max(actual_cost, 0))
            delta = cost - final  # > 0 — вернуть, < 0 — доплатить
            if delta < 0:
                balance = self._conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()[0]
                delta = -min(-delta, max(balance, 0))
                final = cost - delta
            self._conn.execute(
                "UPDATE generations SET status = ?, cost = ? WHERE id = ?",
                ("done" if ok else "failed", final, gen_id),
            )
            if delta:
                self._conn.execute("UPDATE users SET credits = credits + ? WHERE id = ?", (delta, user_id))
            self._conn.execute("COMMIT")
            return final

    # --- настройки пользователя ---

    def get_settings(self, user_id: int) -> dict:
        row = self._conn.execute("SELECT settings FROM users WHERE id = ?", (user_id,)).fetchone()
        try:
            return json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            return {}

    def save_settings(self, user_id: int, settings: dict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE users SET settings = ? WHERE id = ?", (json.dumps(settings, ensure_ascii=False), user_id)
            )

    # --- запуски агента ---

    def add_agent_run(self, run_id: str, user_id: int, gen_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO agent_runs (run_id, user_id, gen_id, created_at) VALUES (?, ?, ?, ?)",
                (run_id, user_id, gen_id, int(time.time())),
            )

    def agent_run(self, run_id: str) -> tuple[int, int] | None:
        """(user_id, gen_id) или None."""
        return self._conn.execute(
            "SELECT user_id, gen_id FROM agent_runs WHERE run_id = ?", (run_id,)
        ).fetchone()

    def agent_run_by_gen(self, gen_id: int) -> tuple[str, int] | None:
        """(run_id, user_id) или None."""
        return self._conn.execute(
            "SELECT run_id, user_id FROM agent_runs WHERE gen_id = ?", (gen_id,)
        ).fetchone()

    def extend(self, gen_id: int, extra: int) -> None:
        """Довнести (или вернуть при extra < 0) кредиты в резерв незавершённой генерации."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT user_id, status FROM generations WHERE id = ?", (gen_id,)).fetchone()
                if row is None or row[1] != "pending":
                    raise ValueError("generation is not pending")
                cur = self._conn.execute(
                    "UPDATE users SET credits = credits - ? WHERE id = ? AND credits >= ?", (extra, row[0], extra))
                if cur.rowcount != 1:
                    raise InsufficientCredits
                self._conn.execute("UPDATE generations SET cost = cost + ? WHERE id = ?", (extra, gen_id))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def generation_cost(self, gen_id: int) -> int:
        row = self._conn.execute("SELECT cost FROM generations WHERE id = ?", (gen_id,)).fetchone()
        return row[0] if row else 0

    def pending_agent_runs(self) -> list[tuple[str, int, int]]:
        """Незавершённые запуски агента (после перезапуска бота их нужно доотследить)."""
        return self._conn.execute(
            "SELECT a.run_id, a.user_id, a.gen_id FROM agent_runs a JOIN generations g ON g.id = a.gen_id"
            " WHERE g.status = 'pending'"
        ).fetchall()

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
            "agent": q("SELECT COUNT(*) FROM generations WHERE kind = 'agent' AND status = 'done'").fetchone()[0],
            "failed": q("SELECT COUNT(*) FROM generations WHERE status = 'failed'").fetchone()[0],
        }
