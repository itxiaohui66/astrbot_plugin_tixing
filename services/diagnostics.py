"""Show the actual incoming mention fields without exposing tokens or full IDs."""

import hashlib
import json

from .delivery import field
from .targets import MARKUP, mention_data, named_recipient, resolve_target

VERSION = "v1.4.0"


def identity(value):
    if not value:
        return ""
    return "id#" + hashlib.sha256(str(value).encode()).hexdigest()[:10]


async def diagnose(event, args, store, scope):
    raw = event.message_obj.raw_message
    data = field(raw, "raw_data", raw)
    trusted, _, aliases, bot_ids, _, payload, sdk = mention_data(event)
    ids = set(aliases) | set(aliases.values()) | set(trusted) | bot_ids
    ids.add(str(event.get_sender_id()))

    def redact(text):
        text = str(text or "")
        text = MARKUP.sub(
            lambda match: (
                "[QQ @ " + identity(next(g for g in match.groups() if g is not None)) + "]"
            ),
            text,
        )
        for value in sorted(ids, key=len, reverse=True):
            if value:
                text = text.replace(value, identity(value))
        return text[:600]

    def member(value):
        # Select only incoming member fields; never serialize bot/api/session objects.
        return {
            key: identity(field(value, key))
            if key in {"id", "member_openid"}
            else field(value, key)
            for key in (
                "id",
                "member_openid",
                "username",
                "nickname",
                "nick",
                "card",
                "is_you",
                "bot",
            )
            if field(value, key) is not None
        }

    report = {
        "插件版本": VERSION,
        "适配器": event.get_platform_name(),
        "消息对象类型": type(raw).__name__,
        "发起人": identity(event.get_sender_id()),
        "AstrBot正文": redact(event.message_str),
        "QQ原始正文": redact(field(data, "content") or field(raw, "content", "")),
        "QQ原始mentions": [member(item) for item in payload[:10]],
        "SDK mentions": [member(item) for item in sdk[:10]],
        "At消息段": [
            {"id": identity(field(c, "qq")), "name": field(c, "name", "")}
            for c in event.get_messages()
            if field(c, "type") in {"At", "at"} or type(c).__name__ == "At"
        ][:10],
        "本群已知名字数": len(await store.member_names(scope)),
        "原始author": member(field(data, "author", {})),
    }
    try:
        parts = args.split(maxsplit=1)
        first = parts[0] if parts else ""
        tail = parts[1] if len(parts) == 2 else ""
        explicit_name = None
        if first in {"to", "给", "提醒他人"}:
            explicit_name, args = named_recipient(tail)
        _, target, name = await resolve_target(event, args, store, scope, explicit_name)
        report["解析结果"] = {
            "对象": identity(target),
            "名字": name,
            "提醒自己": target == str(event.get_sender_id()),
        }
    except ValueError as exc:
        report["解析失败"] = redact(str(exc))
    return json.dumps(report, ensure_ascii=False, indent=2)
