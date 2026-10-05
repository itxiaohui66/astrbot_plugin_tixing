"""Observe the official SDK's group lifecycle events without replacing its parsers."""

import asyncio
import inspect

from astrbot.api import logger

NOTICE = (
    "⏰ 定时提醒需要主动消息权限。请群主或管理员打开本群机器人的权限设置，"
    "确认已开启“允许机器人主动发送消息”（不同 QQ 版本名称可能略有不同）。"
    "开启后可 @机器人 /tx permission check 验证；随后按设置时间主动提醒。"
)
EVENTS = ("group_add_robot", "group_del_robot", "group_msg_receive", "group_msg_reject")


class PermissionBridge:
    def __init__(self, platform, handler):
        self.platform = platform
        self.client = platform.get_client()
        self.handler = handler
        self.closed = False
        self.callbacks = {}
        self.tasks = set()

    def install(self):
        for kind in EVENTS:
            name = "on_" + kind
            previous = getattr(self.client, name, None)

            async def callback(event, kind=kind, previous=previous):
                task = asyncio.current_task()
                if not self.closed:
                    self.tasks.add(task)
                    try:
                        await self.handler(self.platform, kind, event)
                    except Exception:
                        logger.exception("[Reminder] 处理 QQ 群权限事件失败：%s", kind)
                    finally:
                        self.tasks.discard(task)
                if previous:
                    result = previous(event)
                    if inspect.isawaitable(result):
                        await result

            callback._reminder_bridge = self
            callback._reminder_previous = previous
            setattr(self.client, name, callback)
            self.callbacks[name] = callback

    async def close(self):
        self.closed = True
        # Finish any notification + persistence before the plugin is unloaded.
        if self.tasks:
            pending = asyncio.gather(*self.tasks, return_exceptions=True)
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise
        for name, callback in self.callbacks.items():
            if getattr(self.client, name, None) is not callback:
                continue
            previous = callback._reminder_previous
            while previous and isinstance(
                getattr(previous, "_reminder_bridge", None), PermissionBridge
            ):
                if not previous._reminder_bridge.closed:
                    break
                previous = previous._reminder_previous
            if previous is None:
                delattr(self.client, name)
            else:
                setattr(self.client, name, previous)
