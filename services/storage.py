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
                CREATE TABLE IF NOT EXISTS member_names (
                    scope TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL,
                    PRIMARY KEY(scope, user_id, name)
                );
                CREATE INDEX IF NOT EXISTS member_names_lookup ON member_names(scope, name);
                CREATE TABLE IF NOT EXISTS member_aliases (
                    scope TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL,
                    PRIMARY KEY(scope, user_id), UNIQUE(scope, name)
                );
                INSERT OR IGNORE INTO member_names
                    SELECT scope,user_id,nickname FROM members WHERE nickname!='';
                CREATE TABLE IF NOT EXISTS reply_contexts (
                    platform_id TEXT NOT NULL, scene TEXT NOT NULL, destination TEXT NOT NULL,
                    msg_id TEXT NOT NULL, received_at REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(platform_id, scene, destination)
                );
                CREATE TABLE IF NOT EXISTS group_permissions (
                    platform_id TEXT NOT NULL, destination TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'unknown', notified INTEGER NOT NULL DEFAULT 0,
                    notice_until REAL NOT NULL DEFAULT 0, event_at REAL NOT NULL DEFAULT 0,
                    event_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(platform_id, destination)
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(reminders)")}
            if "waiting_reason" not in columns:
                db.execute(
                    "ALTER TABLE reminders ADD COLUMN waiting_reason TEXT NOT NULL DEFAULT ''"
                )

        await self._run(create)

    async def permission(self, platform_id, destination):
        return await self._run(
            lambda db: dict(
                db.execute(
                    "SELECT * FROM group_permissions WHERE platform_id=? AND destination=?",
                    (platform_id, destination),
                ).fetchone()
                or {"state": "unknown", "notified": 0}
            )
        )

    async def set_permission(
        self, platform_id, destination, state, event_at=None, event_id="", reset=False
    ):
        def write(db):
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO group_permissions(platform_id,destination) VALUES (?,?)",
                (platform_id, destination),
            )
            row = db.execute(
                "SELECT * FROM group_permissions WHERE platform_id=? AND destination=?",
                (platform_id, destination),
            ).fetchone()
            if event_at is not None and (
                event_at < row["event_at"] or (event_id and event_id == row["event_id"])
            ):
                return False
            reset_notice = reset and (row["state"] != state or event_at is not None)
            db.execute(
                "UPDATE group_permissions SET state=?,event_at=COALESCE(?,event_at),"
                "event_id=CASE WHEN ?!='' THEN ? ELSE event_id END,"
                "notified=CASE WHEN ? THEN 0 ELSE notified END,"
                "notice_until=CASE WHEN ? THEN 0 ELSE notice_until END "
                "WHERE platform_id=? AND destination=?",
                (
                    state,
                    event_at,
                    event_id,
                    event_id,
                    reset_notice,
                    reset_notice,
                    platform_id,
                    destination,
                ),
            )
            if state == "granted":
                db.execute(
                    "UPDATE reminders SET retry_at=0,waiting_reason='' WHERE platform_id=? "
                    "AND destination=? AND scene='group' AND status='active' "
                    "AND waiting_reason LIKE '主动消息权限%'",
                    (platform_id, destination),
                )
            return True

        return await self._run(write)

    async def claim_notice(self, platform_id, destination):
        def write(db):
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO group_permissions(platform_id,destination) VALUES (?,?)",
                (platform_id, destination),
            )
            return (
                db.execute(
                    "UPDATE group_permissions SET notice_until=? WHERE platform_id=? "
                    "AND destination=? AND state!='granted' AND notified=0 AND notice_until<=?",
                    (time.time() + 60, platform_id, destination, time.time()),
                ).rowcount
                == 1
            )

        return await self._run(write)

    async def finish_notice(self, platform_id, destination, sent):
        await self._run(
            lambda db: db.execute(
                "UPDATE group_permissions SET notified=MAX(notified,?),notice_until=0 WHERE platform_id=? AND destination=?",
                (int(sent), platform_id, destination),
            )
        )

    async def wait_for_permission(self, reminder_id, owner, reason, retry_at):
        await self._run(
            lambda db: db.execute(
                "UPDATE reminders SET waiting_reason=?,retry_at=?,lease_owner='',lease_until=0 "
                "WHERE id=? AND lease_owner=? AND status='active'",
                (reason[:500], retry_at, reminder_id, owner),
            )
        )

    async def observe(self, scope, user_id, nickname="", names=()):
        await self.observe_members(scope, [(user_id, nickname, names)])

    async def observe_members(self, scope, members):
        def write(db):
            for user_id, nickname, names in members:
                if nickname == user_id:
                    nickname = ""
                names = list(dict.fromkeys(n for n in (nickname, *names) if n and n != user_id))
                db.execute(
                    "INSERT INTO members VALUES (?,?,?,?) ON CONFLICT(scope,user_id) DO UPDATE SET "
                    "nickname=CASE WHEN excluded.nickname!='' THEN excluded.nickname ELSE members.nickname END, "
                    "seen_at=excluded.seen_at",
                    (scope, user_id, nickname or "", time.time()),
                )
                if names:
                    # Keep current names, rather than matching a nickname after a rename.
                    db.execute(
                        "DELETE FROM member_names WHERE scope=? AND user_id=?", (scope, user_id)
                    )
                    db.executemany(
                        "INSERT INTO member_names VALUES (?,?,?)",
                        [(scope, user_id, name) for name in names],
                    )

        await self._run(write)

    async def member_names(self, scope):
        return await self._run(
            lambda db: [
                r[0]
                for r in db.execute(
                    "SELECT name FROM member_names WHERE scope=? UNION "
                    "SELECT name FROM member_aliases WHERE scope=?",
                    (scope, scope),
                )
            ]
        )

    async def member(self, scope, value):
        def read(db):
            rows = db.execute(
                "SELECT DISTINCT m.* FROM members m LEFT JOIN member_names n "
                "ON m.scope=n.scope AND m.user_id=n.user_id "
                "LEFT JOIN member_aliases a ON m.scope=a.scope AND m.user_id=a.user_id "
                "WHERE m.scope=? AND (m.user_id=? OR n.name=? OR a.name=?)",
                (scope, value, value, value),
            ).fetchall()
            exact = [r for r in rows if r["user_id"] == value]
            if exact:
                return dict(exact[0])
            if len(rows) > 1:
                raise ValueError(
                    "本群有重名用户，请让成员 /tx name 登记不同提醒昵称，或使用平台能识别的真实 @。"
                )
            return dict(rows[0]) if rows else None

        return await self._run(read)

    async def register_name(self, scope, user_id, name):
        def write(db):
            db.execute("BEGIN IMMEDIATE")
            occupied = db.execute(
                "SELECT user_id FROM member_names WHERE scope=? AND name=? UNION "
                "SELECT user_id FROM member_aliases WHERE scope=? AND name=? UNION "
                "SELECT user_id FROM members WHERE scope=? AND user_id=?",
                (scope, name, scope, name, scope, name),
            ).fetchall()
            if any(row[0] != user_id for row in occupied):
                raise ValueError("该名字已经对应本群其他成员，请选择一个唯一的提醒昵称。")
            db.execute(
                "INSERT INTO member_aliases VALUES (?,?,?) "
                "ON CONFLICT(scope,user_id) DO UPDATE SET name=excluded.name",
                (scope, user_id, name),
            )

        await self._run(write)

    async def registered_name(self, scope, user_id):
        def read(db):
            row = db.execute(
                "SELECT name FROM member_aliases WHERE scope=? AND user_id=?", (scope, user_id)
            ).fetchone()
            return row["name"] if row else ""

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
            sql = "UPDATE reminders SET status='cancelled', waiting_reason='', lease_owner='', lease_until=0 WHERE scope=? AND status='active'"
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
                    "AND retry_at<=? AND lease_until<=? ORDER BY remind_at,id",
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
                    "last_error='',waiting_reason='',retry_at=0,lease_owner='',lease_until=0 WHERE id=? AND lease_owner=? AND status='active'",
                    ("active" if next_time is not None else "done", next_time, reminder_id, owner),
                ).rowcount
            )
        )

    async def failure(self, reminder_id, owner, error, maximum, retry_at):
        def write(db):
            db.execute(
                "UPDATE reminders SET failure_count=failure_count+1,last_error=?,retry_at=?,waiting_reason='',"
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

    async def remember_reply(self, platform_id, scene, destination, msg_id, received_at):
        # Duplicate/replayed events must not renew expiry or refill reply quota.
        await self._run(
            lambda db: (
                db.execute(
                    "INSERT INTO reply_contexts(platform_id,scene,destination,msg_id,received_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(platform_id,scene,destination) DO UPDATE SET "
                    "msg_id=excluded.msg_id,received_at=excluded.received_at,used=0,blocked=0 "
                    "WHERE reply_contexts.msg_id!=excluded.msg_id AND excluded.received_at>=reply_contexts.received_at",
                    (platform_id, scene, destination, msg_id, received_at),
                ).rowcount
            )
        )

    async def reserve_reply(self, platform_id, scene, destination, now, window):
        def reserve(db):
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM reply_contexts WHERE platform_id=? AND scene=? AND destination=?",
                (platform_id, scene, destination),
            ).fetchone()
            # Reserve two of QQ's five replies for command responses/other plugins.
            if (
                not row
                or row["blocked"]
                or row["used"] >= 3
                or not 0 <= now - row["received_at"] < window
            ):
                return None
            db.execute(
                "UPDATE reply_contexts SET used=used+1 WHERE platform_id=? AND scene=? AND destination=?",
                (platform_id, scene, destination),
            )
            # AstrBot's official event sender uses random sequences in 1..10000.
            return {"msg_id": row["msg_id"], "msg_seq": 20001 + row["used"]}

        return await self._run(reserve)

    async def block_reply(self, platform_id, scene, destination, msg_id):
        await self._run(
            lambda db: (
                db.execute(
                    "UPDATE reply_contexts SET blocked=1 WHERE platform_id=? AND scene=? AND destination=? AND msg_id=?",
                    (platform_id, scene, destination, msg_id),
                ).rowcount
            )
        )

    async def wait_for_reply(self, reminder_id, owner, reason):
        await self._run(
            lambda db: (
                db.execute(
                    "UPDATE reminders SET waiting_reason=?,retry_at=0,lease_owner='',lease_until=0 "
                    "WHERE id=? AND lease_owner=? AND status='active'",
                    (reason[:500], reminder_id, owner),
                ).rowcount
            )
        )

    async def resume(self, scope, creator_id, reminder_id, limit):
        def write(db):
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM reminders WHERE scope=? AND id=?", (scope, reminder_id)
            ).fetchone()
            if not row or (creator_id is not None and row["creator_id"] != creator_id):
                raise ValueError("当前会话中找不到该提醒，或你没有操作权限。")
            if row["status"] == "done" or (row["status"] == "cancelled" and not row["last_error"]):
                raise ValueError("只能重试投递失败或正在等待的提醒。")
            if row["lease_until"] > time.time():
                raise ValueError("该提醒正在投递，请稍后重试。")
            count = db.execute(
                "SELECT COUNT(*) FROM reminders WHERE scope=? AND creator_id=? AND status='active' AND id!=?",
                (scope, row["creator_id"], reminder_id),
            ).fetchone()[0]
            if count >= limit:
                raise ValueError(f"最多只能设置 {limit} 个活跃提醒，请先取消一些。")
            db.execute(
                "UPDATE reminders SET status='active',failure_count=0,last_error='',waiting_reason='',"
                "retry_at=0,lease_owner='',lease_until=0 WHERE id=?",
                (reminder_id,),
            )

        await self._run(write)
