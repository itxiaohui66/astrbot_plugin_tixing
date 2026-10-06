"""Resolve real mention components, QQ official markup and observed member names."""

import re

from .delivery import OFFICIAL, field

MARKUP = re.compile(
    r"<qqbot-at-user\s+id=[\"\']([^\"\']+)[\"\']\s*/>|<@!?([^>]+)>|\[CQ:at,qq=([^,\]]+)[^\]]*\]"
)
VALID_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def member_names(member):
    return list(
        dict.fromkeys(
            str(field(member, key, "") or "").strip()
            for key in ("card", "nick", "nickname", "username", "name")
            if field(member, key)
        )
    )


def mention_data(event):
    bot_ids = {str(event.message_obj.self_id)}
    raw = event.message_obj.raw_message
    raw_data = field(raw, "raw_data", raw)
    # botpy may expose a nonempty mentions list whose objects have lost id,
    # is_you or username. It must not hide the adapter's original QQ payload.
    payload_mentions = field(raw_data, "mentions", []) or []
    sdk_mentions = field(raw, "mentions", []) or []
    mentions = list(payload_mentions)
    if raw_data is not raw:
        mentions.extend(sdk_mentions)
    trusted = {}
    names = {}
    bot_names = set()
    aliases = {}
    official_group = event.get_platform_name() in OFFICIAL and bool(
        field(raw, "group_openid") or field(raw_data, "group_openid")
    )
    # Learn alias mappings from complete raw records before processing SDK objects.
    for mention in mentions:
        generic_id = str(field(mention, "id", "") or "")
        member_id = str(field(mention, "member_openid", "") or "")
        canonical = (member_id or generic_id) if official_group else (generic_id or member_id)
        if canonical:
            for value in (generic_id, member_id):
                if value:
                    aliases.setdefault(value, canonical)
    for mention in mentions:
        generic_id = str(field(mention, "id", "") or "")
        member_id = str(field(mention, "member_openid", "") or "")
        user = (member_id or generic_id) if official_group else (generic_id or member_id)
        user = aliases.get(user, user)
        if field(mention, "is_you", False) or field(mention, "bot", False):
            bot_ids.update({user, generic_id, member_id} - {""})
            bot_names.update(member_names(mention))
        elif user and user not in bot_ids:
            values = member_names(mention)
            names.setdefault(user, []).extend(values)
            trusted[user] = trusted.get(user) or next(iter(values), "")
    trusted = {user: name for user, name in trusted.items() if user not in bot_ids}
    # Read only this message's original content, never a quoted message. Some
    # adapters normalize away mention tokens from message_str / message components.
    if event.get_platform_name() in OFFICIAL:
        content = field(raw_data, "content", None) or field(raw, "content", "") or ""
        for match in MARKUP.finditer(str(content)):
            if not match.group(0).startswith("<"):
                continue
            raw_user = next(g for g in match.groups() if g is not None)
            user = aliases.get(raw_user, raw_user)
            if user not in bot_ids and VALID_ID.fullmatch(user):
                trusted.setdefault(user, "")
    for component in event.get_messages():
        if field(component, "type") in {"At", "at"} or type(component).__name__ == "At":
            raw_user = str(field(component, "qq", ""))
            user = aliases.get(raw_user, raw_user)
            if user and user not in bot_ids:
                trusted[user] = trusted.get(user) or str(field(component, "name", "") or "")
                names.setdefault(user, []).extend(member_names(component))
            elif user in bot_ids:
                bot_names.update(member_names(component))
    return trusted, names, aliases, bot_ids, bot_names, payload_mentions, sdk_mentions


async def observe_members(event, store, scope):
    """Learn names only from the sender and native mentions supplied by QQ."""
    trusted, names, *_ = mention_data(event)
    raw = event.message_obj.raw_message
    raw_data = field(raw, "raw_data", raw)
    sender = str(event.get_sender_id())
    sender_names = []
    for source in (raw_data, raw):
        sender_names.extend(member_names(field(source, "member", {})))
        sender_names.extend(member_names(field(source, "sender", {})))
        sender_names.extend(member_names(field(source, "author", {})))
    display = str(event.get_sender_name() or "")
    if display and display != sender:
        sender_names.append(display)
    rows = [(user, name, names.get(user, [])) for user, name in trusted.items() if user != sender]
    rows.append((sender, next(iter(sender_names), ""), sender_names))
    await store.observe_members(scope, rows)


def text_mentions(names):
    # Longest known name first supports spaces in nicknames without consuming
    # reminder text after a shorter name. Quoted names work before registration.
    known = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True) if n)
    pattern = re.compile(
        r'(?<![A-Za-z0-9_.@＠])[@＠](?:"([^"\n]+)"|“([^”\n]+)”|'
        + (rf"({known})(?=\s|$)|" if known else "")
        + r"([^\s@]+))"
    )
    return pattern


async def resolve_target(event, args, store, scope):
    sender = str(event.get_sender_id())
    trusted, names, aliases, bot_ids, bot_names, payload_mentions, sdk_mentions = mention_data(
        event
    )
    candidates = list(trusted)
    for match in MARKUP.finditer(args):
        raw_user = next(g for g in match.groups() if g is not None)
        user = aliases.get(raw_user, raw_user)
        if user not in bot_ids:
            candidates.append(user)
    args = MARKUP.sub(" ", args)
    known_names = await store.member_names(scope)
    pattern = text_mentions(
        known_names + [n for values in names.values() for n in values] + list(bot_names)
    )
    textual = list(pattern.finditer(args))
    # A QQ mention can precede the command. Without native mention metadata,
    # tx()'s argument slicing previously discarded this explicit recipient.
    command = re.search(r"(?:^|\s)/?(?:tx|提醒|remind)(?=\s|$)", event.message_str, re.I)
    if command:
        prefix = MARKUP.sub(" ", event.message_str[: command.start()])
        textual.extend(pattern.finditer(prefix))
    for match in textual:
        value = next(g for g in match.groups() if g is not None)
        if value in bot_names or aliases.get(value, value) in bot_ids:
            continue
        # @all must not be allowed as a recipient.
        if value in {"all", "everyone", "全体成员"}:
            raise ValueError("一次提醒只能指定一个成员，不能 @ 全体成员。")
        matching = [user for user in trusted if value in names.get(user, [])]
        if len(matching) > 1:
            raise ValueError("被 @ 成员重名，请在 QQ 的 @ 群成员列表中只选中一个人。")
        # A real QQ selection identifies the member even when cached names collide.
        member = None if matching else await store.member(scope, value)
        if (
            not matching
            and not member
            and aliases.get(value, value) not in trusted
            and event.get_platform_name() in OFFICIAL
        ):
            raise ValueError(
                f"还未识别本群成员“{value}”，未创建提醒。请输入 @ 后从 QQ 群成员列表中选中对方；"
                "也可以让对方先在本群 @机器人发一条消息，再用 @昵称。无需填写 OpenID。"
            )
        candidates.append(
            matching[0] if matching else member["user_id"] if member else aliases.get(value, value)
        )
    args = pattern.sub(" ", args).strip()
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        unknown = [
            mention
            for mention in payload_mentions
            if not (
                field(mention, "id")
                or field(mention, "member_openid")
                or field(mention, "is_you", False)
                or field(mention, "bot", False)
            )
        ]
        sdk_unknown = [
            mention
            for mention in sdk_mentions
            if not (
                field(mention, "id")
                or field(mention, "member_openid")
                or field(mention, "is_you", False)
                or field(mention, "bot", False)
            )
        ]
        if unknown or (not payload_mentions and sdk_unknown and len(sdk_mentions) > 1):
            raise ValueError(
                "收到 @ 成员信息，但无法识别提醒对象，未创建提醒。"
                "请输入 @ 后从 QQ 群成员列表中选中对方，或让对方先 @机器人发一条消息再用 @昵称。"
            )
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
                    "无法识别本群提醒对象，未创建提醒。请输入 @ 后从 QQ 群成员列表中选中对方，"
                    "或让对方先 @机器人发一条消息再用 @昵称。无需填写 OpenID。"
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
