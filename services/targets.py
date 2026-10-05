"""Resolve real mention components, QQ official markup and observed member names."""

import re

from .delivery import OFFICIAL, field

MARKUP = re.compile(
    r"<qqbot-at-user\s+id=[\"\']([^\"\']+)[\"\']\s*/>|<@!?([^>]+)>|\[CQ:at,qq=([^,\]]+)[^\]]*\]"
)
TEXT_AT = re.compile(r"(?<![\w@])@([^\s@]+)")
VALID_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


async def resolve_target(event, args, store, scope):
    sender = str(event.get_sender_id())
    bot_ids = {str(event.message_obj.self_id)}
    raw = event.message_obj.raw_message
    raw_data = field(raw, "raw_data", raw)
    mentions = field(raw, "mentions", None) or field(raw_data, "mentions", []) or []
    trusted = {}
    aliases = {}
    official_group = event.get_platform_name() in OFFICIAL and bool(
        field(raw, "group_openid") or field(raw_data, "group_openid")
    )
    for mention in mentions:
        generic_id = str(field(mention, "id", "") or "")
        member_id = str(field(mention, "member_openid", "") or "")
        user = (member_id or generic_id) if official_group else (generic_id or member_id)
        if user:
            for value in (generic_id, member_id):
                if value:
                    aliases[value] = user
        if field(mention, "is_you", False) or field(mention, "bot", False):
            bot_ids.update({user, generic_id, member_id} - {""})
        elif user:
            trusted[user] = str(field(mention, "username", "") or "")
    for component in event.get_messages():
        if field(component, "type") in {"At", "at"} or type(component).__name__ == "At":
            raw_user = str(field(component, "qq", ""))
            user = aliases.get(raw_user, raw_user)
            if user and user not in bot_ids:
                trusted[user] = trusted.get(user) or str(field(component, "name", "") or "")
    candidates = list(trusted)
    for match in MARKUP.finditer(args):
        raw_user = next(g for g in match.groups() if g is not None)
        user = aliases.get(raw_user, raw_user)
        if user not in bot_ids:
            candidates.append(user)
    args = MARKUP.sub(" ", args)
    textual = list(TEXT_AT.finditer(args))
    for match in textual:
        value = match.group(1)
        # @all must not be allowed as a recipient.
        if value in {"all", "everyone", "全体成员"}:
            raise ValueError("一次提醒只能指定一个成员，不能 @ 全体成员。")
        names = [user for user, name in trusted.items() if name and name == value]
        if len(names) > 1:
            raise ValueError("被 @ 成员重名，请使用用户标识。")
        member = await store.member(scope, value)
        candidates.append(
            names[0] if names else member["user_id"] if member else aliases.get(value, value)
        )
    args = TEXT_AT.sub(" ", args).strip()
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) > 1:
        raise ValueError("一次提醒只能指定一个成员。")
    target = candidates[0] if candidates else sender
    if not VALID_ID.fullmatch(target) or target == "all":
        raise ValueError("提醒对象标识无效，请使用真实 @。")
    if not event.get_group_id() and target != sender:
        raise ValueError("私聊只能提醒自己；提醒他人请在群内使用。")
    member = await store.member(scope, target)
    if target != sender:
        if event.get_platform_name() in OFFICIAL:
            if target not in trusted and not member:
                raise ValueError(
                    "无法确认该用户的本群 OpenID。请真实 @ 对方；若官方未提供成员标识，"
                    "让对方先在本群 @ 机器人发送 /tx identity，再用 @用户标识。普通 QQ 号不能转换成 OpenID。"
                )
        elif event.get_platform_name() == "aiocqhttp":
            try:
                await event.bot.get_group_member_info(
                    group_id=int(event.get_group_id()), user_id=int(target), no_cache=True
                )
            except Exception as exc:
                raise ValueError("无法在本群找到该成员，请重新 @ 群成员。") from exc
    name = trusted.get(target) or (member["nickname"] if member else "")
    if target == sender:
        name = event.get_sender_name() or ""
    return args, target, name
