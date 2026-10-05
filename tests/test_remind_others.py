import time
from types import SimpleNamespace

import pytest
from test_reminders import plugin as plugin
from test_reminders import record

from services.targets import resolve_target


def recipient_event(event_factory, platform, source):
    bot = {"id": "bot_id", "is_you": True}
    person = {"id": "target_generic", "member_openid": "target_member", "username": "小明"}
    if source == "lossy_sdk":
        payload = {
            "group_openid": "group_1",
            "mentions": [bot, person],
            "content": '/tx 1分钟 开会 <qqbot-at-user id="target_generic" />',
        }
        raw = SimpleNamespace(
            group_openid="group_1",
            raw_data=payload,
            mentions=[SimpleNamespace(member_openid=None), SimpleNamespace(member_openid=None)],
        )
    elif source == "raw_content":
        raw = SimpleNamespace(
            group_openid="group_1",
            mentions=[],
            content=(
                '<qqbot-at-user id="bot_id" /> /tx 1分钟 开会 <qqbot-at-user id="target_member" />'
            ),
        )
    else:
        raw = {
            "group_openid": "group_1",
            "content": (
                '<qqbot-at-user id="bot_id" /> <qqbot-at-user id="target_member" /> /tx 1分钟 开会'
            ),
        }
    event = event_factory("/tx 1分钟 开会", platform=platform, raw=raw)
    # The adapter exposes only the triggering bot mention, not the other member.
    event.messages = [SimpleNamespace(qq="bot_id", name="机器人", type="At")]
    return event


@pytest.mark.parametrize("platform_name", ["qq_official", "qq_official_webhook"])
@pytest.mark.parametrize("source", ["lossy_sdk", "raw_content", "at_before_command"])
async def test_other_recipient_is_saved_and_delivered_after_reload(
    plugin,
    plugin_class,
    event_factory,
    platform_name,
    source,
):
    instance, platform = plugin
    platform.meta = lambda: SimpleNamespace(id="platform_1", name=platform_name)
    event = recipient_event(event_factory, platform_name, source)
    await instance.tx(event)
    rows = await instance.store.list(record()["scope"])
    assert len(rows) == 1
    rem = rows[0]
    assert rem["creator_id"] == "user_1"
    assert rem["target_id"] == "target_member" and "提醒对象" in event.replies[0]
    assert rem["content"] == "开会"
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
        sent = platform.get_client().api.post_group_message.call_args.kwargs
        body = sent["markdown"]["content"]
        assert body.startswith('<qqbot-at-user id="target_member" />\n\n')
        assert '<qqbot-at-user id="user_1"' not in body
        assert body.count("qqbot-at-user") == 1
        assert (await restored.store.get(rem["id"], record()["scope"]))["status"] == "done"
    finally:
        await restored.terminate()


async def test_partial_sdk_mentions_do_not_create_second_recipient(plugin, event_factory):
    instance, _ = plugin
    raw = SimpleNamespace(
        group_openid="group_1",
        mentions=[SimpleNamespace(id="target_generic")],
        raw_data={
            "group_openid": "group_1",
            "mentions": [
                {"id": "target_generic", "member_openid": "target_member", "username": "小明"}
            ],
        },
    )
    event = event_factory(raw=raw)
    _, target, name = await resolve_target(event, "1分钟 开会", instance.store, record()["scope"])
    assert target == "target_member" and name == "小明"


async def test_sdk_lost_bot_flag_cannot_turn_triggering_bot_into_recipient(plugin, event_factory):
    instance, _ = plugin
    raw = SimpleNamespace(
        group_openid="group_1",
        mentions=[SimpleNamespace(id="bot_generic"), SimpleNamespace(id="target_generic")],
        raw_data={
            "group_openid": "group_1",
            "mentions": [
                {"id": "bot_generic", "member_openid": "bot_member", "is_you": True},
                {"id": "target_generic", "member_openid": "target_member", "username": "小明"},
            ],
        },
    )
    event = event_factory(raw=raw)
    _, target, name = await resolve_target(event, "1分钟 开会", instance.store, record()["scope"])
    assert target == "target_member" and name == "小明"


async def test_missing_target_metadata_is_not_silently_self_reminder(plugin, event_factory):
    instance, _ = plugin
    event = event_factory(
        raw=SimpleNamespace(
            group_openid="group_1",
            mentions=[SimpleNamespace(member_openid=None), SimpleNamespace(member_openid=None)],
        )
    )
    event.messages = [SimpleNamespace(qq="bot_id", name="机器人", type="At")]
    await instance.tx(event)
    assert "未创建提醒" in event.replies[0]
    assert not await instance.store.list(record()["scope"])


async def test_quoted_mentions_do_not_change_recipient(plugin, event_factory):
    instance, _ = plugin
    event = event_factory(
        raw={
            "group_openid": "group_1",
            "content": "/tx 1分钟 开会",
            "message_type": 103,
            "msg_elements": [{"content": '<qqbot-at-user id="quoted_user" />'}],
        }
    )
    event.messages = [
        SimpleNamespace(qq="bot_id", name="机器人", type="At"),
        SimpleNamespace(type="Reply", chain=[SimpleNamespace(qq="quoted_user", type="At")]),
    ]
    await instance.tx(event)
    rem = (await instance.store.list(record()["scope"]))[0]
    assert rem["target_id"] == rem["creator_id"] == "user_1"
    assert "提醒对象：你自己" in event.replies[0]
