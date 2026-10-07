import time
from types import SimpleNamespace

import pytest
from test_reminders import plugin as plugin
from test_reminders import record

from services.diagnostics import identity
from services.targets import command_args


def official_event(event_factory, text, user="user_1", normalized=None, platform="qq_official"):
    # Reproduce the user's QQ payload: no mentions, only the triggering bot At.
    event = event_factory(
        normalized or text.lstrip("/"),
        user=user,
        platform=platform,
        raw=SimpleNamespace(
            group_openid="group_1",
            mentions=[],
            raw_data={
                "group_openid": "group_1",
                "content": " " + text,
                "mentions": [],
                "author": {"id": user, "member_openid": user, "username": "QQ昵称_" + user},
            },
        ),
    )
    event.messages = [SimpleNamespace(qq="bot_id", name="", type="At")]
    return event


@pytest.mark.parametrize("platform_name", ["qq_official", "qq_official_webhook"])
@pytest.mark.parametrize("name", ["xh", '"小 明"'])
async def test_register_and_remind_by_name_without_incoming_mentions_or_typed_ids(
    plugin, plugin_class, event_factory, platform_name, name
):
    instance, platform = plugin
    platform.meta = lambda: SimpleNamespace(id="platform_1", name=platform_name)
    register = official_event(
        event_factory, f"/tx name {name}", "target_member", platform=platform_name
    )
    await instance.tx(register)
    assert "登记给你自己" in register.replies[0]
    assert "target_member" not in register.replies[0]
    # A later message / rename must not erase the explicitly registered nickname.
    await instance.observe_member(
        official_event(event_factory, "你好", "target_member", platform=platform_name)
    )
    await instance.terminate()
    restored = plugin_class(instance.context, {})
    await restored.store.initialize()
    restored._ready = True
    try:
        event = official_event(event_factory, f"/tx to {name} 1分钟 测试", platform=platform_name)
        await restored.tx(event)
        rem = (await restored.store.list(record()["scope"]))[0]
        assert rem["creator_id"] == "user_1" and rem["target_id"] == "target_member"
        assert rem["target_name"] == name.strip('"') and rem["content"] == "测试"
        assert "你自己" not in event.replies[0] and "target_member" not in event.replies[0]
        await restored.store._run(
            lambda db: db.execute(
                "UPDATE reminders SET remind_at=? WHERE id=?", (time.time() - 1, rem["id"])
            )
        )
        await restored._check_due()
        sent = platform.get_client().api.post_group_message.call_args.kwargs["markdown"]["content"]
        assert sent.startswith('<qqbot-at-user id="target_member" />')
        assert 'id="user_1"' not in sent
    finally:
        await restored.terminate()


async def test_raw_textual_nickname_survives_normalized_command_loss(plugin, event_factory):
    instance, _ = plugin
    await instance.store.observe(record()["scope"], "target_member", "xh")
    event = official_event(event_factory, "/tx 1分钟 测试 @xh", normalized="/tx 1分钟 测试")
    assert command_args(event) == "1分钟 测试 @xh"
    await instance.tx(event)
    assert (await instance.store.list(record()["scope"]))[0]["target_id"] == "target_member"


async def test_unknown_name_never_creates_self_reminder_and_guides_registration(
    plugin, event_factory
):
    instance, _ = plugin
    for text in ("/tx to xh 1分钟 测试", "/tx 1分钟 测试 @xh"):
        event = official_event(event_factory, text)
        await instance.tx(event)
        assert "未创建提醒" in event.replies[0] and "/tx name xh" in event.replies[0]
        assert "无需复制 ID" in event.replies[0]
        assert not await instance.store.list(record()["scope"])


async def test_fully_stripped_at_cannot_be_inferred_and_self_confirmation_warns(
    plugin, event_factory
):
    instance, _ = plugin
    event = official_event(event_factory, "/tx 1分钟 测试")
    await instance.tx(event)
    assert "本次未识别到其他成员" in event.replies[0] and "/tx to" in event.replies[0]


async def test_registration_is_only_for_sender_and_isolated_by_group(plugin, event_factory):
    instance, _ = plugin
    register = official_event(event_factory, "/tx name xh", "target_member")
    await instance.tx(register)
    occupied = official_event(event_factory, "/tx name xh", "another_user")
    await instance.tx(occupied)
    assert "已经对应本群其他成员" in occupied.replies[0]
    assert (await instance.store.member(record()["scope"], "xh"))["user_id"] == "target_member"
    event = event_factory("/tx to xh 1分钟 测试", group="other_group")
    await instance.tx(event)
    assert "未创建提醒" in event.replies[0]


async def test_change_registration_releases_old_alias_without_changing_existing_reminders(
    plugin, event_factory
):
    instance, _ = plugin
    number = await instance.store.add(record(target_id="target_member", target_name="xh"), 10)
    for name in ("xh", "xh新名字"):
        await instance.tx(official_event(event_factory, f"/tx name {name}", "target_member"))
    assert await instance.store.member(record()["scope"], "xh") is None
    assert (await instance.store.member(record()["scope"], "xh新名字"))[
        "user_id"
    ] == "target_member"
    saved = await instance.store.get(number, record()["scope"])
    assert saved["target_id"] == "target_member" and saved["target_name"] == "xh"


async def test_known_permission_failure_is_visible_on_new_named_reminder(plugin, event_factory):
    instance, _ = plugin
    await instance.store.observe(record()["scope"], "target_member", "xh")
    await instance.store.set_permission("platform_1", "group_1", "error")
    event = official_event(event_factory, "/tx to xh 1分钟 测试")
    await instance.tx(event)
    assert "提醒对象：xh" in event.replies[0]
    assert "当前已检测到主动发送受限" in event.replies[0]
    assert "/tx permission check" in event.replies[0]


async def test_registered_literal_at_nickname_works_without_mention_payload(plugin, event_factory):
    instance, _ = plugin
    await instance.tx(official_event(event_factory, "/tx name xh", "target_member"))
    await instance.tx(official_event(event_factory, "/tx 1分钟 测试 @xh"))
    saved = (await instance.store.list(record()["scope"]))[0]
    assert saved["target_id"] == "target_member" and saved["target_name"] == "xh"


async def test_registration_conflicts_with_observed_other_member_name(plugin, event_factory):
    instance, _ = plugin
    await instance.store.observe(record()["scope"], "another_member", "xh")
    event = official_event(event_factory, "/tx name xh", "target_member")
    await instance.tx(event)
    assert "已经对应" in event.replies[0]


async def test_explicit_name_must_agree_with_native_selection(plugin, event_factory):
    instance, _ = plugin
    await instance.store.observe(record()["scope"], "target_member", "xh")
    event = event_factory(
        "/tx to xh 1分钟 测试",
        raw={
            "group_openid": "group_1",
            "mentions": [{"member_openid": "different_member", "username": "另一个人"}],
        },
    )
    await instance.tx(event)
    assert "只能指定一个成员" in event.replies[0]
    assert not await instance.store.list(record()["scope"])


async def test_bot_cannot_be_explicit_named_recipient(plugin, event_factory):
    instance, _ = plugin
    event = event_factory("/tx to bot_id 1分钟 测试")
    await instance.tx(event)
    assert "不能是机器人" in event.replies[0]
    assert not await instance.store.list(record()["scope"])


async def test_named_diagnostic_and_registered_name_query(plugin, event_factory):
    instance, _ = plugin
    await instance.tx(official_event(event_factory, "/tx name xh", "target_member"))
    event = official_event(event_factory, "/tx diagnose to xh 1分钟 测试")
    await instance.tx(event)
    assert identity("target_member") in event.replies[0]
    assert not await instance.store.list(record()["scope"])
    event = official_event(event_factory, "/tx name", "target_member")
    await instance.tx(event)
    assert "你的提醒昵称：xh" in event.replies[0]


async def test_group_registration_not_accepted_in_private(plugin, event_factory):
    instance, _ = plugin
    event = event_factory("/tx name xh", group="")
    await instance.tx(event)
    assert "群内登记" in event.replies[0]


@pytest.mark.parametrize("tail", ["", "@xh", "<@id>", "all", "x" * 65])
async def test_invalid_registration_rejected(plugin, event_factory, tail):
    instance, _ = plugin
    event = official_event(event_factory, f"/tx name {tail}")
    await instance.tx(event)
    assert "登记给你自己" not in event.replies[0]
    assert not await instance.store.registered_name(record()["scope"], "user_1")
