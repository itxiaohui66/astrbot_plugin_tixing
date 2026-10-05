"""Deliver through the current adapter using a validated incoming reply context."""

import time
from datetime import datetime
from html import escape

OFFICIAL = {"qq_official", "qq_official_webhook"}


def proactive_denied(exc):
    text = str(exc).lower()
    return any(
        token in text
        for token in ("主动消息失败", "无权限", "permission", "not allowed", "主动消息权限")
    )


def incoming_reply(event):
    """Read an inbound message ID and its original timestamp, never a sent ID."""
    raw = event.message_obj.raw_message
    data = field(raw, "raw_data", raw)
    msg_id = str(
        field(raw, "id") or field(data, "id") or field(event.message_obj, "message_id", "") or ""
    )
    if not msg_id:
        return None
    timestamp = (
        field(raw, "timestamp") or field(data, "timestamp") or field(event.message_obj, "timestamp")
    )
    try:
        received_at = float(timestamp)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            received_at = parsed.timestamp()
        except (TypeError, ValueError):
            return None
    return {"msg_id": msg_id, "received_at": min(received_at, time.time())}


def reply_unavailable(exc):
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "22009",
            "msg limit exceed",
            "msg_id",
            "msg id",
            "msg_seq",
            "message expired",
            "message is expired",
            "消息过期",
            "消息已过期",
            "被动消息",
            "回复超",
            "超频",
            "无权限",
            "permission",
            "主动消息失败",
        )
    )


def field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def route(event):
    group = str(event.get_group_id() or "")
    raw = event.message_obj.raw_message
    data = field(raw, "raw_data", raw)
    if event.get_platform_name() in OFFICIAL:
        if field(raw, "group_openid") or field(data, "group_openid"):
            return "group", str(field(raw, "group_openid") or field(data, "group_openid"))
        if field(raw, "channel_id") or field(data, "channel_id"):
            # Guild DMs need a separate guild ID, not the user's OpenID.
            if not group:
                return "guild_dm", str(field(raw, "guild_id") or field(data, "guild_id") or "")
            return "channel", str(field(raw, "channel_id") or field(data, "channel_id"))
        if not group:
            return "c2c", str(event.get_sender_id())
        raise ValueError("无法识别官方 QQ 群消息来源，请检查 AstrBot 适配器版本。")
    return ("group", group) if group else ("private", str(event.get_sender_id()))


def find_platform(context, platform_id):
    manager = context.platform_manager
    return next((p for p in manager.platform_insts if str(p.meta().id) == platform_id), None)


def available(platform):
    if platform is None:
        return False
    status = field(field(platform, "status"), "value", "running")
    # An absent/starting/disconnected platform does not count as a failed delivery.
    return str(status).lower() == "running"


def reminder_text(rem):
    creator = rem["creator_name"] or rem["creator_id"]
    prefix = f"来自 {creator} 的提醒：\n" if rem["target_id"] != rem["creator_id"] else ""
    return f"{prefix}⏰ {rem['content']}\n（提醒 #{rem['id']}）"


async def deliver(platform, rem, reply=None):
    """Official SDK return value must contain a message ID to count as sent."""
    client = platform.get_client()
    text = reminder_text(rem)
    if rem["platform_name"] in OFFICIAL:
        api = client.api
        scene = rem["scene"]
        kwargs = dict(reply or {})
        if scene not in {"group", "c2c"}:
            kwargs.pop("msg_seq", None)
        if scene == "group":
            mention = f'<qqbot-at-user id="{escape(rem["target_id"], quote=True)}" />'
            result = await api.post_group_message(
                group_openid=rem["destination"], msg_type=0, content=f"{mention}\n{text}", **kwargs
            )
        elif scene == "c2c":
            result = await api.post_c2c_message(
                openid=rem["destination"], msg_type=0, content=text, **kwargs
            )
        elif scene == "channel":
            result = await api.post_message(
                channel_id=rem["destination"], content=f"<@{rem['target_id']}>\n{text}", **kwargs
            )
        elif scene == "guild_dm":
            result = await api.post_dms(guild_id=rem["destination"], content=text, **kwargs)
        else:
            raise ValueError(f"未知官方 QQ 投递场景：{scene}")
        if not field(result, "id"):
            raise RuntimeError(
                "QQ 官方发送接口未返回消息 ID，请检查主动消息权限、额度和适配器版本。"
            )
        return
    # Preserve old OneBot behavior, including a real @ and member validation.
    if rem["platform_name"] == "aiocqhttp":
        message = [{"type": "text", "data": {"text": text}}]
        if rem["scene"] == "group":
            message.insert(0, {"type": "at", "data": {"qq": rem["target_id"]}})
            result = await client.send_group_msg(group_id=int(rem["destination"]), message=message)
        else:
            result = await client.send_private_msg(user_id=int(rem["destination"]), message=message)
        if not field(result, "message_id"):
            raise RuntimeError("OneBot 发送接口未返回消息 ID。")
        return
    raise ValueError(f"不支持的提醒平台：{rem['platform_name']}")
