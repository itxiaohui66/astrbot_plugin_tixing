import time
from types import SimpleNamespace

import pytest
from test_reminders import plugin as plugin
from test_reminders import record

from services.storage import ReminderStore
from services.targets import observe_members, resolve_target


@pytest.mark.parametrize("platform_name", ["qq_official", "qq_official_webhook"])
@pytest.mark.parametrize("name", ["小明", "群名片", "小 明", '"小 明"', "“小 明”"])
async def test_nickname_creation_persists_and_mentions_other_on_delivery(
    plugin, plugin_class, event_factory, platform_name, name
):
    instance, platform = plugin
    platform.meta = lambda: SimpleNamespace(id="platform_1", name=platform_name)
    # Learn a member's actual group name and username from a received message.
    member_event = event_factory(
        "你好",
        user="target_member",
        platform=platform_name,
        raw=SimpleNamespace(
            group_openid="group_1",
            author=SimpleNamespace(member_openid="target_member"),
            raw_data={
                "author": {"member_openid": "target_member", "username": "小明"},
                "member": {"nick": "群名片", "card": "小 明"},
            },
        ),
    )
    await instance.observe_member(member_event)
    # The directory must survive plugin updates/restarts before creation too.
    await instance.store.initialize()
    event = event_factory(f"/tx 1分钟 开会 @{name}", platform=platform_name)
    await instance.tx(event)
    rows = await instance.store.list(record()["scope"])
    assert len(rows) == 1
    rem = rows[0]
    assert rem["target_id"] == "target_member" and rem["creator_id"] == "user_1"
    assert rem["content"] == "开会"
    assert "target_member" not in event.replies[0]
    await instance.store._run(
        lambda db: db.execute(
            "UPDATE reminders SET remind_at=? WHERE id=?", (time.time() - 1, rem["id"])
        )
    )
    await instance.terminate()
    restored = plugin_class(instance.context, {})
    await restored.store.initialize()
    try:
        await restored._check_due()
        body = platform.get_client().api.post_group_message.call_args.kwargs["markdown"]["content"]
        assert body.startswith('<qqbot-at-user id="target_member" />')
        assert body.count("qqbot-at-user") == 1 and 'id="user_1"' not in body
    finally:
        await restored.terminate()


async def test_native_selection_wins_over_duplicate_cached_nickname(plugin, event_factory):
    instance, _ = plugin
    scope = record()["scope"]
    await instance.store.observe(scope, "another_member", "小明")
    event = event_factory(
        "/tx 1分钟 开会 @小明",
        raw={
            "group_openid": "group_1",
            "mentions": [
                {"id": "generic_target", "member_openid": "target_member", "username": "小明"}
            ],
        },
    )
    await instance.tx(event)
    assert (await instance.store.list(scope))[0]["target_id"] == "target_member"


async def test_mentions_are_learned_even_if_member_has_never_spoken(plugin, event_factory):
    instance, _ = plugin
    event = event_factory(
        "普通消息",
        raw=SimpleNamespace(
            group_openid="group_1",
            mentions=[SimpleNamespace(member_openid=None)],
            raw_data={
                "mentions": [
                    {
                        "id": "generic_target",
                        "member_openid": "target_member",
                        "username": "小明",
                        "nickname": "群名片",
                    }
                ]
            },
        ),
    )
    await instance.observe_member(event)
    assert not event.stopped
    for name in ("小明", "群名片"):
        _, target, _ = await resolve_target(
            event_factory(), f"1分钟 开会 @{name}", instance.store, record()["scope"]
        )
        assert target == "target_member"


async def test_unknown_or_cross_group_nickname_never_creates_self_reminder(plugin, event_factory):
    instance, _ = plugin
    await instance.store.observe("another_scope", "target_member", "小明")
    event = event_factory("/tx 1分钟 开会 @小明")
    await instance.tx(event)
    assert "未创建提醒" in event.replies[0] and "群成员列表" in event.replies[0]
    assert "/tx identity" not in event.replies[0]
    assert not await instance.store.list(record()["scope"])


async def test_rename_removes_old_alias_and_empty_payload_keeps_current_names(
    tmp_path, event_factory
):
    store = ReminderStore(tmp_path / "members.db")
    await store.initialize()
    scope = record()["scope"]
    await store.observe(scope, "target_member", "旧名", ["旧名片"])
    await store.observe(scope, "target_member", "新名", ["新名片"])
    await store.observe(scope, "target_member", "")
    await store.initialize()
    assert await store.member(scope, "旧名") is None
    assert await store.member(scope, "旧名片") is None
    assert (await store.member(scope, "新名片"))["user_id"] == "target_member"


async def test_existing_database_nicknames_upgrade_without_losing_tasks(tmp_path):
    store = ReminderStore(tmp_path / "members.db")
    await store.initialize()
    reminder_id = await store.add(record(), 10)
    await store._run(
        lambda db: db.execute(
            "INSERT INTO members VALUES (?,?,?,?)",
            (record()["scope"], "target_member", "原昵称", time.time()),
        )
    )
    await store.initialize()
    assert (await store.member(record()["scope"], "原昵称"))["user_id"] == "target_member"
    assert (await store.get(reminder_id, record()["scope"]))["content"] == "吃饭"


async def test_bot_display_name_is_not_a_recipient(plugin, event_factory):
    instance, _ = plugin
    event = event_factory(
        "/tx 1分钟 开会 @小明 @栗子",
        raw={
            "group_openid": "group_1",
            "mentions": [
                {"id": "bot_id", "is_you": True, "username": "栗子"},
                {"id": "target_member", "username": "小明"},
            ],
        },
    )
    await instance.tx(event)
    assert (await instance.store.list(record()["scope"]))[0]["target_id"] == "target_member"


async def test_at_component_name_is_learned(plugin, event_factory):
    instance, _ = plugin
    event = event_factory("普通消息")
    event.messages = [SimpleNamespace(qq="target_member", name="小明", type="At")]
    await observe_members(event, instance.store, record()["scope"])
    assert (await instance.store.member(record()["scope"], "小明"))["user_id"] == "target_member"


@pytest.mark.parametrize(
    "text",
    [
        "/tx 1分钟 开会@小明",
        "/tx 1分钟 开会＠小明",
        "@小明 /tx 1分钟 开会",
        "@小明 /提醒 1分钟 开会",
        "@小明 /remind 1分钟 开会",
    ],
)
async def test_adjacent_and_before_command_nicknames_are_recipients(plugin, event_factory, text):
    instance, platform = plugin
    await instance.store.observe(record()["scope"], "target_member", "小明")
    event = event_factory(text)
    await instance.tx(event)
    rem = (await instance.store.list(record()["scope"]))[0]
    assert rem["target_id"] == "target_member" and rem["content"] == "开会"
    await instance.store._run(
        lambda db: db.execute(
            "UPDATE reminders SET remind_at=? WHERE id=?", (time.time() - 1, rem["id"])
        )
    )
    await instance._check_due()
    body = platform.get_client().api.post_group_message.call_args.kwargs["markdown"]["content"]
    assert body.startswith('<qqbot-at-user id="target_member" />') and 'id="user_1"' not in body


async def test_adjacent_unknown_nickname_is_not_silently_self(plugin, event_factory):
    instance, _ = plugin
    event = event_factory("/tx 1分钟 开会@陌生人")
    await instance.tx(event)
    assert "未创建提醒" in event.replies[0]
    assert not await instance.store.list(record()["scope"])


async def test_email_in_reminder_content_is_not_a_recipient(plugin, event_factory):
    instance, _ = plugin
    event = event_factory("/tx 1分钟 给 test@example.com 发邮件")
    await instance.tx(event)
    rem = (await instance.store.list(record()["scope"]))[0]
    assert rem["target_id"] == rem["creator_id"]
    assert rem["content"] == "给 test@example.com 发邮件"
