"""Transactional SQLite persistence outside the installed plugin directory."""

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def scope_key(platform_id, group_id, user_id):
    # Group UMO may include the sender when unique-session is enabled.
    return json.dumps([platform_id, "group" if group_id else "private", group_id or user_id])


class ReminderStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def _run(self, operation):
        def run():
            with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                db.row_factory = sqlite3.Row
                return operation(db)

        async with self._lock:
            # Wait for the worker before releasing the lock even during unload.
            worker = asyncio.create_task(asyncio.to_thread(run))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise

    async def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def create(db):
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL, platform_id TEXT NOT NULL,
                    platform_name TEXT NOT NULL, destination TEXT NOT NULL,
                    scene TEXT NOT NULL, umo TEXT NOT NULL,
                    creator_id TEXT NOT NULL, creator_name TEXT NOT NULL DEFAULT '',
                    target_id TEXT NOT NULL, target_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL, remind_at REAL NOT NULL,
                    timezone TEXT NOT NULL, repeat_type TEXT NOT NULL
                        CHECK(repeat_type IN ('none','daily','weekly')),
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','done','cancelled')),
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    retry_at REAL NOT NULL DEFAULT 0,
                    lease_owner TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, remind_at);
                CREATE INDEX IF NOT EXISTS reminders_scope ON reminders(scope, creator_id);
                CREATE TABLE IF NOT EXISTS members (
                    scope TEXT NOT NULL, user_id TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '', seen_at REAL NOT NULL,
                    PRIMARY KEY(scope, user_id)
                );
            """)

        await self._run(create)

    async def observe(self, scope, user_id, nickname=""):
        await self._run(
            lambda db: (
                db.execute(
                    "INSERT INTO members VALUES (?,?,?,?) ON CONFLICT(scope,user_id) DO UPDATE SET "
                    "nickname=CASE WHEN excluded.nickname!='' THEN excluded.nickname ELSE members.nickname END, "
                    "seen_at=excluded.seen_at",
                    (scope, user_id, nickname or "", time.time()),
                ).rowcount
            )
        )

    async def member(self, scope, value):
        def read(db):
            rows = db.execute(
                "SELECT * FROM members WHERE scope=? AND (user_id=? OR nickname=?)",
                (scope, value, value),
            ).fetchall()
            exact = [r for r in rows if r["user_id"] == value]
            if exact:
                return dict(exact[0])
            if len(rows) > 1:
                raise ValueError("本群有重名用户，请使用真实 @ 或 /tx identity 获取的用户标识。")
            return dict(rows[0]) if rows else None

        return await self._run(read)

    async def add(self, record, limit):
        def insert(db):
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT COUNT(*) FROM reminders WHERE scope=? AND creator_id=? AND status='active'",
                (record["scope"], record["creator_id"]),
            ).fetchone()[0]
            if count >= limit:
                raise ValueError(f"你在当前会话最多只能设置 {limit} 个提醒，请先取消一些。")
            columns = (
                "scope",
                "platform_id",
                "platform_name",
                "destination",
                "scene",
                "umo",
                "creator_id",
                "creator_name",
                "target_id",
                "target_name",
                "content",
                "remind_at",
                "timezone",
                "repeat_type",
            )
            cursor = db.execute(
                f"INSERT INTO reminders ({','.join(columns)},created_at) "
                f"VALUES ({','.join('?' for _ in range(len(columns) + 1))})",
                tuple(record[c] for c in columns) + (time.time(),),
            )
            return cursor.lastrowid

        return await self._run(insert)

    async def list(self, scope, creator_id=None, history=False):
        def read(db):
            sql, params = "SELECT * FROM reminders WHERE scope=?", [scope]
            if creator_id is not None:
                sql += " AND creator_id=?"
                params.append(creator_id)
            if not history:
                sql += " AND status='active'"
            sql += " ORDER BY remind_at,id"
            return [dict(r) for r in db.execute(sql, params).fetchall()]

        return await self._run(read)

    async def get(self, reminder_id, scope):
        def read(db):
            row = db.execute(
                "SELECT * FROM reminders WHERE id=? AND scope=?", (reminder_id, scope)
            ).fetchone()
            return dict(row) if row else None

        return await self._run(read)

    async def cancel(self, scope, creator_id, reminder_id=None):
        def write(db):
            sql = "UPDATE reminders SET status='cancelled', lease_owner='', lease_until=0 WHERE scope=? AND status='active'"
            params = [scope]
            if creator_id is not None:
                sql += " AND creator_id=?"
                params.append(creator_id)
            if reminder_id is not None:
                sql += " AND id=?"
                params.append(reminder_id)
            return db.execute(sql, params).rowcount

        return await self._run(write)

    async def due(self, now):
        return await self._run(
            lambda db: [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM reminders WHERE status='active' AND remind_at<=? "
                    "AND retry_at<=? AND lease_until<=? ORDER BY remind_at,id LIMIT 100",
                    (now, now, now),
                )
            ]
        )

    async def claim(self, reminder_id, owner, now):
        # One lease per send prevents overlapping hot reloads from sending twice.
        return await self._run(
            lambda db: (
                db.execute(
                    "UPDATE reminders SET lease_owner=?,lease_until=? WHERE id=? AND status='active' "
                    "AND lease_until<=? AND remind_at<=? AND retry_at<=?",
                    (owner, now + 120, reminder_id, now, now, now),
                ).rowcount
                == 1
            )
        )

    async def success(self, reminder_id, owner, next_time=None):
        return await self._run(
            lambda db: (
                db.execute(
                    "UPDATE reminders SET status=?,remind_at=COALESCE(?,remind_at),failure_count=0,"
                    "last_error='',retry_at=0,lease_owner='',lease_until=0 WHERE id=? AND lease_owner=? AND status='active'",
                    ("active" if next_time is not None else "done", next_time, reminder_id, owner),
                ).rowcount
            )
        )

    async def failure(self, reminder_id, owner, error, maximum, retry_at):
        def write(db):
            db.execute(
                "UPDATE reminders SET failure_count=failure_count+1,last_error=?,retry_at=?,"
                "status=CASE WHEN failure_count+1>=? THEN 'cancelled' ELSE 'active' END,"
                "lease_owner='',lease_until=0 WHERE id=? AND lease_owner=? AND status='active'",
                (str(error)[:500], retry_at, maximum, reminder_id, owner),
            )

        await self._run(write)

    async def release(self, reminder_id, owner):
        await self._run(
            lambda db: (
                db.execute(
                    "UPDATE reminders SET lease_owner='',lease_until=0 WHERE id=? AND lease_owner=?",
                    (reminder_id, owner),
                ).rowcount
            )
        )
