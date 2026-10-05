"""Complete reminder migration from liziqqbotpy to AstrBot."""

import asyncio
import contextlib
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .services.delivery import (
    OFFICIAL,
    available,
    deliver,
    field,
    find_platform,
    incoming_reply,
    proactive_denied,
    reply_unavailable,
    route,
)
from .services.permissions import NOTICE, PermissionBridge
from .services.storage import ReminderStore, scope_key
from .services.targets import MARKUP, resolve_target
from .services.time_parser import describe, next_occurrence, parse_time

HELP = """⏰ 栗子提醒
/tx 30分钟 开会（也支持分钟后、小时、天、min/h/d）
/tx 1小时 开会 @某人
/tx 10:00 开会（今天）
/tx 明天10:00 开会
/tx 2026-12-31 20:00 跨年
/tx 每天9:00 早起打卡
/tx 每周一 9:00 周报（也支持周日、周天、1-7）
/tx list [页码] 查看自己当前会话的提醒
/tx history [页码] 查看历史和失败原因
/tx detail 编号 查看一条提醒
/tx cancel 编号 取消自己的一条提醒
/tx cancel all 取消自己当前会话的全部提醒
/tx retry 编号 恢复投递失败的提醒
/tx identity 查看当前平台、群和用户标识
/tx status 查看提醒调度器状态
/tx permission 查看主动消息权限说明
/tx permission check 发送主动消息验证权限
/cxzd 管理员重启提醒调度器
命令别名：/提醒、/remind
群内官方 QQ 需要 @机器人；时间按配置时区计算。
官方 QQ 提醒他人须使用本群 OpenID 或真实 @；普通 QQ 号不可转换。
默认到点主动提醒；群主/管理员需开启“允许机器人主动发送消息”。
管理员可 /tx list all 和 /tx cancel 编号 管理当前群提醒。"""


class ReminderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.timezone = str(config.get("timezone", "Asia/Shanghai"))
        try:
            self.tz = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"无效提醒时区：{self.timezone}，请使用 Asia/Shanghai 等 IANA 时区"
            ) from exc
        self.limit = max(1, int(config.get("max_per_user", 10)))
        self.interval = max(1, int(config.get("check_interval_seconds", 30)))
        self.max_failures = max(1, int(config.get("max_delivery_failures", 3)))
        self.retry_seconds = max(1, int(config.get("retry_interval_seconds", 60)))
        self.max_content = max(1, min(1500, int(config.get("max_content_length", 1000))))
        # v1.2 replaces the old v1.1 passive default with a new configuration key.
        self.delivery_mode = "proactive" if config.get("proactive_reminders", True) else "passive"
        self._wake = asyncio.Event()
        self.store = ReminderStore(StarTools.get_data_dir("astrbot_plugin_tixing") / "reminders.db")
        self.owner = uuid.uuid4().hex
        self._ready = False
        self._closed = False
        self._init_lock = asyncio.Lock()
        self._scan_lock = asyncio.Lock()
        self._scheduler = None
        self._permission_watcher = None
        self._bridges = {}
        self.last_scan = 0
        self.last_scan_error = ""

    async def initialize(self):
        async with self._init_lock:
            if self._closed:
                return
            if not self._ready:
                await self.store.initialize()
                self._ready = True
            await self._attach_platforms()
            if self._permission_watcher is None or self._permission_watcher.done():
                self._permission_watcher = asyncio.create_task(self._watch_platforms())
            if self._scheduler is None or self._scheduler.done():
                self._scheduler = asyncio.create_task(self._poll(), name="lizi-reminders")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        await self.initialize()

    async def _attach_platforms(self):
        live = set()
        for platform in self.context.platform_manager.platform_insts:
            if platform.meta().name not in OFFICIAL:
                continue
            client = platform.get_client()
            if client is None:
                continue
            key = id(client)
            live.add(key)
            if key not in self._bridges:
                bridge = PermissionBridge(platform, self._permission_event)
                bridge.install()
                self._bridges[key] = bridge
        for key in set(self._bridges) - live:
            await self._bridges.pop(key).close()

    async def _watch_platforms(self):
        while True:
            await asyncio.sleep(5)
            try:
                await self._attach_platforms()
            except Exception:
                logger.exception("[Reminder] 连接 QQ 群权限事件失败，将继续重试")

    async def _permission_event(self, platform, kind, event):
        if self._closed:
            return
        destination = str(field(event, "group_openid") or "")
        if not destination:
            return
        platform_id = str(platform.meta().id)
        event_id = str(field(event, "event_id") or "")
        stamp = field(event, "timestamp")
        try:
            event_at = float(stamp)
        except (ValueError, TypeError):
            try:
                event_at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                event_at = time.time()
        states = {
            "group_add_robot": "unknown",
            "group_del_robot": "removed",
            "group_msg_receive": "granted",
            "group_msg_reject": "denied",
        }
        changed = await self.store.set_permission(
            platform_id,
            destination,
            states[kind],
            event_at,
            event_id,
            reset=kind in {"group_add_robot", "group_del_robot", "group_msg_reject"},
        )
        if not changed:
            return
        if kind == "group_msg_receive":
            self._wake.set()
        if kind != "group_add_robot" or not event_id or self.delivery_mode != "proactive":
            return
        if not await self.store.claim_notice(platform_id, destination):
            return
        sent = False
        try:
            # A real join event can be replied to even before proactive permission is enabled.
            result = await asyncio.wait_for(
                platform.get_client().api.post_group_message(
                    group_openid=destination,
                    msg_type=0,
                    content=NOTICE,
                    event_id=event_id,
                    msg_seq=20001,
                ),
                timeout=30,
            )
            sent = bool(field(result, "id"))
            if not sent:
                logger.warning("[Reminder] 入群权限提示没有返回消息 ID，将在首次命令时提示")
        except Exception as exc:
            logger.warning("[Reminder] 入群权限提示失败，将在首次命令时提示：%s", exc)
        finally:
            await self.store.finish_notice(platform_id, destination, sent)

    async def _poll(self):
        while True:
            try:
                await self._check_due()
                self.last_scan_error = ""
            except Exception as exc:
                self.last_scan_error = str(exc)[:300]
                logger.exception("[Reminder] 扫描提醒失败，将在下一轮继续")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _check_due(self):
        async with self._scan_lock:
            self.last_scan = time.time()
            for rem in await self.store.due(self.last_scan):
                platform = find_platform(self.context, rem["platform_id"])
                if not available(platform):
                    continue
                if not await self.store.claim(rem["id"], self.owner, time.time()):
                    continue
                reply = None
                if (
                    rem["platform_name"] in OFFICIAL
                    and self.delivery_mode == "proactive"
                    and rem["scene"] == "group"
                ):
                    permission = await self.store.permission(rem["platform_id"], rem["destination"])
                    if permission["state"] in {"denied", "removed"}:
                        await self.store.wait_for_permission(
                            rem["id"],
                            self.owner,
                            "主动消息权限未开启或机器人已退群；请管理员开启后 /tx permission check 验证",
                            time.time() + 300,
                        )
                        continue
                if rem["platform_name"] in OFFICIAL and self.delivery_mode == "passive":
                    # QQ documents group/channel/DM windows of 5 min, C2C 60 min.
                    # Leave a margin for the 30 s send timeout.
                    window = 3540 if rem["scene"] == "c2c" else 240
                    reply = await self.store.reserve_reply(
                        rem["platform_id"], rem["scene"], rem["destination"], time.time(), window
                    )
                    if reply is None:
                        await self.store.wait_for_reply(
                            rem["id"],
                            self.owner,
                            rem.get("waiting_reason")
                            or "没有有效回复窗口或本条消息回复额度已用完，等待下次 @机器人 补发",
                        )
                        continue
                # On unload, finish the current send + persistence before exiting.
                # A send has a 30 s timeout, shorter than the 120 s DB lease.
                task = asyncio.create_task(self._fire(platform, rem, reply))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise

    async def _fire(self, platform, rem, reply=None):
        try:
            await asyncio.wait_for(deliver(platform, rem, reply), timeout=30)
        except Exception as exc:
            if reply is None and rem["platform_name"] in OFFICIAL and proactive_denied(exc):
                if rem["scene"] == "group":
                    await self.store.set_permission(
                        rem["platform_id"], rem["destination"], "error", reset=True
                    )
                await self.store.wait_for_permission(
                    rem["id"],
                    self.owner,
                    f"主动消息权限受限：{exc}；请管理员开启后 /tx permission check 验证，任务保留并每 5 分钟重试",
                    time.time() + 300,
                )
                logger.warning(
                    "[Reminder] 提醒 #%s 等待主动消息权限，任务已保留：%s", rem["id"], exc
                )
                return
            if reply is not None and reply_unavailable(exc):
                await self.store.block_reply(
                    rem["platform_id"], rem["scene"], rem["destination"], reply["msg_id"]
                )
                await self.store.wait_for_reply(
                    rem["id"], self.owner, f"被动回复受限：{exc}；等待下次 @机器人 补发"
                )
                logger.info("[Reminder] 提醒 #%s 等待新的可回复消息：%s", rem["id"], exc)
                return
            await self.store.failure(
                rem["id"], self.owner, exc, self.max_failures, time.time() + self.retry_seconds
            )
            logger.warning(
                "[Reminder] 提醒 #%s 投递失败（%s/%s）：%s",
                rem["id"],
                rem["failure_count"] + 1,
                self.max_failures,
                exc,
            )
            return
        # A DB write failure after successful delivery is a distinct failure.
        # Keep the lease rather than incrementing the delivery-failure count.
        next_time = None
        if rem["repeat_type"] in {"daily", "weekly"}:
            next_time = next_occurrence(
                rem["remind_at"], rem["repeat_type"], rem["timezone"], time.time()
            )
        await self.store.success(rem["id"], self.owner, next_time)
        if reply is None and rem["platform_name"] in OFFICIAL and rem["scene"] == "group":
            await self.store.set_permission(rem["platform_id"], rem["destination"], "granted")
        logger.info("[Reminder] 提醒 #%s 已投递，平台=%s", rem["id"], rem["platform_id"])

    def _admin(self, event):
        return (
            event.is_admin()
            or str(event.get_sender_id())
            in {str(user) for user in self.config.get("admin_ids", [])}
            or f"{event.get_platform_id()}|{event.get_group_id()}|{event.get_sender_id()}"
            in self.config.get("group_admin_ids", [])
        )

    @staticmethod
    def _scope(event):
        return scope_key(
            str(event.get_platform_id()),
            str(event.get_group_id() or ""),
            str(event.get_sender_id()),
        )

    async def _reply(self, event, text):
        # Passive command responses are sent by AstrBot using the current event.
        destination = ""
        if event.get_platform_name() in OFFICIAL and self.delivery_mode == "proactive":
            try:
                scene, dest = route(event)
            except ValueError:
                scene, dest = "", ""
            if scene == "group" and await self.store.claim_notice(
                str(event.get_platform_id()), dest
            ):
                destination = dest
                if NOTICE not in text:
                    text += "\n\n" + NOTICE
        sent = False
        try:
            await event.send(event.plain_result(text))
            sent = True
        finally:
            if destination:
                await self.store.finish_notice(str(event.get_platform_id()), destination, sent)

    async def _guard(self, event, action):
        try:
            await self.initialize()
            await self._remember_incoming(event)
            await action()
        except ValueError as exc:
            await self._reply(event, f"❌ {exc}")
        except Exception:
            logger.exception("[Reminder] 处理提醒命令失败")
            await self._reply(event, "❌ 处理失败，请稍后重试或查看 AstrBot 日志。")
        finally:
            event.stop_event()

    @filter.command("tx", alias={"提醒", "remind"})
    async def tx(self, event: AstrMessageEvent):
        """设置、查看、取消单次或循环提醒。"""
        # Raw text preserves spaces/newlines in reminder content. Remove only the bot mention.
        text = event.message_str
        bot_id = str(event.message_obj.self_id)
        text = MARKUP.sub(
            lambda m: " " if next(g for g in m.groups() if g is not None) == bot_id else m.group(0),
            text,
        )
        match = re.search(r"(?:^|\s)/?(?:tx|提醒|remind)(?=\s|$)", text, re.I)
        args = text[match.end() :].strip() if match else ""
        await self._guard(event, lambda: self._command(event, args))

    async def _command(self, event, args):
        if not args or args.lower() in {"help", "帮助"}:
            await self._reply(event, HELP)
            return
        if event.get_platform_name() not in OFFICIAL | {"aiocqhttp"}:
            raise ValueError("当前插件支持 QQ 官方 WebSocket、Webhook 和 OneBot。")
        scope = self._scope(event)
        user = str(event.get_sender_id())
        await self.store.observe(scope, user, event.get_sender_name())
        parts = args.split(maxsplit=1)
        action = parts[0].lower()
        tail = parts[1].strip() if len(parts) == 2 else ""
        if action in {"list", "列表", "查看", "history", "历史"}:
            await self._list(event, scope, user, tail, action in {"history", "历史"})
            return
        if action in {"cancel", "取消"}:
            await self._cancel(event, scope, user, tail)
            return
        if action in {"retry", "重试"}:
            if not tail.isdecimal():
                raise ValueError("用法：/tx retry 编号")
            async with self._scan_lock:
                await self.store.resume(
                    scope, None if self._admin(event) else user, int(tail), self.limit
                )
            self._wake.set()
            await self._reply(event, f"✅ 提醒 #{tail} 已恢复，将按当前发送方式重试投递。")
            return
        if action in {"permission", "权限"}:
            await self._permission_command(event, tail)
            return
        if action in {"detail", "详情"}:
            if not tail.isdecimal():
                raise ValueError("用法：/tx detail 编号")
            rem = await self.store.get(int(tail), scope)
            if not rem or (rem["creator_id"] != user and not self._admin(event)):
                raise ValueError("当前会话中找不到该提醒，或你没有查看权限。")
            await self._reply(event, self._format(rem, detail=True))
            return
        if action in {"identity", "身份"}:
            await self._reply(
                event,
                f"平台：{event.get_platform_id()}\n群标识：{event.get_group_id() or '私聊'}\n用户标识：{user}\n本群管理员授权标识：{event.get_platform_id()}|{event.get_group_id()}|{user}",
            )
            return
        if action in {"status", "状态"}:
            reminders = await self.store.list(scope, None if self._admin(event) else user)
            running = self._scheduler is not None and not self._scheduler.done()
            last = (
                datetime.fromtimestamp(self.last_scan, self.tz).strftime("%Y-%m-%d %H:%M:%S")
                if self.last_scan
                else "尚未扫描"
            )
            await self._reply(
                event,
                f"调度器：{'运行中' if running else '停止'}\n当前会话活跃提醒：{len(reminders)}\n扫描间隔：{self.interval} 秒\n时区：{self.timezone}\n最近扫描：{last}\n官方发送方式：{self.delivery_mode}"
                + (f"\n扫描错误：{self.last_scan_error}" if self.last_scan_error else ""),
            )
            return
        args, target, target_name = await resolve_target(event, args, self.store, scope)
        parsed = parse_time(args, datetime.now(self.tz))
        if len(parsed.content) > self.max_content:
            raise ValueError(f"提醒内容最多 {self.max_content} 个字符。")
        scene, destination = route(event)
        if not destination:
            raise ValueError("无法获取当前会话的投递目标。")
        record = {
            "scope": scope,
            "platform_id": str(event.get_platform_id()),
            "platform_name": event.get_platform_name(),
            "destination": destination,
            "scene": scene,
            "umo": event.unified_msg_origin,
            "creator_id": user,
            "creator_name": event.get_sender_name() or "",
            "target_id": target,
            "target_name": target_name,
            "content": parsed.content,
            "remind_at": parsed.when.timestamp(),
            "timezone": self.timezone,
            "repeat_type": parsed.repeat,
        }
        reminder_id = await self.store.add(record, self.limit)
        message = f"⏰ 提醒已设置 #{reminder_id}\n时间：{describe(record['remind_at'], parsed.repeat, self.timezone)}\n时区：{self.timezone}\n内容：{parsed.content}"
        if target != user:
            message += f"\n提醒对象：{target_name or target}"
        if event.get_platform_name() in OFFICIAL:
            if self.delivery_mode == "passive":
                message += "\n使用被动回复：到期有有效回复窗口时发送，否则等待下次 @机器人 补发。"
            else:
                message += "\n到点主动提醒；权限受限时保留任务，等待开启权限后补发。"
        await self._reply(event, message)

    def _format(self, rem, detail=False):
        status = {"active": "待提醒", "done": "已完成", "cancelled": "已取消"}[rem["status"]]
        if rem["status"] == "active" and rem.get("waiting_reason"):
            status = (
                "等待主动消息权限"
                if rem["waiting_reason"].startswith("主动消息权限")
                else "等待可回复消息"
            )
        content = (
            rem["content"]
            if detail
            else rem["content"][:200]
            + ("…（/tx detail 查看全文）" if len(rem["content"]) > 200 else "")
        )
        text = f"#{rem['id']} [{status}] {describe(rem['remind_at'], rem['repeat_type'], rem['timezone'])}\n{content}"
        if rem["target_id"] != rem["creator_id"]:
            text += f"\n对象：{rem['target_name'] or rem['target_id']}"
        if detail:
            text += f"\n创建者：{rem['creator_name'] or rem['creator_id']}\n时区：{rem['timezone']}\n失败次数：{rem['failure_count']}"
        if rem["last_error"]:
            text += f"\n最近投递失败：{rem['last_error'][:150]}"
        if rem.get("waiting_reason"):
            text += f"\n等待原因：{rem['waiting_reason'][:150]}"
        return text

    async def _permission_command(self, event, tail):
        if event.get_platform_name() not in OFFICIAL or route(event)[0] != "group":
            raise ValueError("主动消息群权限检查仅适用于 QQ 官方群聊。")
        platform_id = str(event.get_platform_id())
        destination = route(event)[1]
        if not tail:
            state = (await self.store.permission(platform_id, destination))["state"]
            labels = {
                "unknown": "尚未验证",
                "granted": "已收到允许事件或发送成功记录",
                "denied": "收到管理员关闭权限事件",
                "removed": "机器人已被移出群",
                "error": "最近一次主动发送被拒绝",
            }
            await self._reply(event, f"本群状态：{labels[state]}\n{NOTICE}")
            return
        if tail.lower() not in {"check", "检查", "验证"}:
            raise ValueError("用法：/tx permission 或 /tx permission check")
        platform = find_platform(self.context, platform_id)
        if not available(platform):
            raise ValueError("QQ 适配器尚未运行，请稍后再验证。")
        try:
            result = await asyncio.wait_for(
                platform.get_client().api.post_group_message(
                    group_openid=destination,
                    msg_type=0,
                    content="✅ 主动消息权限验证成功。此消息为主动发送，等待权限的到期提醒将继续投递。",
                ),
                timeout=30,
            )
            if not field(result, "id"):
                raise RuntimeError("QQ 接口未返回消息 ID")
        except Exception as exc:
            if proactive_denied(exc):
                await self.store.set_permission(platform_id, destination, "error", reset=True)
            await self._reply(event, f"❌ 主动消息验证失败：{str(exc)[:250]}\n{NOTICE}")
            return
        await self.store.set_permission(platform_id, destination, "granted")
        self._wake.set()

    async def _list(self, event, scope, user, tail, history):
        all_users = tail.split()[:1] == ["all"]
        if all_users:
            if not self._admin(event):
                raise ValueError("仅管理员可以查看当前会话全部用户的提醒。")
            tail = tail[3:].strip()
        if tail and not tail.isdecimal():
            raise ValueError("用法：/tx list [页码] 或 /tx history [页码]")
        page = int(tail or "1")
        if page < 1:
            raise ValueError("页码必须大于 0。")
        rows = await self.store.list(scope, None if all_users else user, history=history)
        if not rows:
            await self._reply(
                event, "当前会话没有提醒记录。" if history else "当前会话没有待提醒任务。"
            )
            return
        # List previews are truncated; detail shows the entire reminder.
        pages = (len(rows) + 1) // 2
        if page > pages:
            raise ValueError(f"总共 {pages} 页。")
        await self._reply(
            event,
            f"📋 提醒{'历史' if history else '列表'}（{page}/{pages}）\n"
            + "\n\n".join(self._format(r) for r in rows[(page - 1) * 2 : page * 2]),
        )

    async def _cancel(self, event, scope, user, tail):
        if tail.lower() in {"all", "全部"}:
            reminder_id = None
            creator = user
        elif tail.isdecimal():
            reminder_id = int(tail)
            creator = None if self._admin(event) else user
        else:
            raise ValueError("用法：/tx cancel 编号 或 /tx cancel all")
        # A confirmed cancellation cannot race a local in-flight delivery.
        async with self._scan_lock:
            count = await self.store.cancel(scope, creator, reminder_id)
        await self._reply(
            event,
            f"✅ 已取消 {count} 条提醒。" if count else "未找到可取消的提醒，或你没有操作权限。",
        )

    @filter.command("cktx", alias={"提醒列表"})
    async def list_reminders(self, event: AstrMessageEvent):
        """查看自己当前会话的待提醒任务。"""
        await self._guard(event, lambda: self._command(event, "list"))

    @filter.command("qxtx", alias={"取消提醒"})
    async def cancel_reminder(self, event: AstrMessageEvent):
        """通过编号取消提醒。"""
        match = re.search(r"(?:^|\s)/?(?:qxtx|取消提醒)(?=\s|$)", event.message_str)
        tail = event.message_str[match.end() :].strip() if match else ""
        await self._guard(event, lambda: self._command(event, "cancel " + tail))

    @filter.command("cxzd", alias={"重启提醒"})
    async def restart_scheduler(self, event: AstrMessageEvent):
        """管理员重启提醒调度器，保留数据库内所有任务。"""

        async def restart():
            if not self._admin(event):
                raise ValueError("只有机器人管理员或配置授权的管理员可以重启提醒调度器。")
            async with self._init_lock:
                if self._scheduler:
                    self._scheduler.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._scheduler
                self._scheduler = asyncio.create_task(self._poll(), name="lizi-reminders")
            await self._reply(
                event, f"✅ 提醒调度器已重启\n时区：{self.timezone}\n扫描间隔：{self.interval} 秒"
            )

        await self._guard(event, restart)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def observe_member(self, event: AstrMessageEvent):
        """Remember group participants for OpenID/name resolution without intercepting chat."""
        if event.get_platform_name() not in OFFICIAL | {"aiocqhttp"}:
            return
        try:
            await self.initialize()
            await self._remember_incoming(event)
            if event.get_group_id():
                await self.store.observe(
                    self._scope(event), str(event.get_sender_id()), event.get_sender_name()
                )
        except Exception:
            logger.exception("[Reminder] 记录本群成员标识失败")

    async def _remember_incoming(self, event):
        if event.get_platform_name() not in OFFICIAL:
            return
        incoming = incoming_reply(event)
        if incoming is None:
            return
        scene, destination = route(event)
        if not destination:
            return
        await self.store.remember_reply(
            str(event.get_platform_id()), scene, destination, **incoming
        )
        self._wake.set()

    async def terminate(self):
        async with self._init_lock:
            self._closed = True
            if self._permission_watcher:
                self._permission_watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._permission_watcher
                self._permission_watcher = None
            if self._scheduler:
                self._scheduler.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._scheduler
                self._scheduler = None
            for bridge in self._bridges.values():
                await bridge.close()
            self._bridges.clear()
