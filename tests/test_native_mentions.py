from types import SimpleNamespace

import pytest
from test_reminders import plugin as plugin
from test_reminders import record

from services.delivery import MentionDeliveryError, deliver, reminder_markdown, reminder_text
from services.storage import ReminderStore
from services.targets import resolve_target


@pytest.mark.parametrize("name", ["qq_official", "qq_official_webhook"])
@pytest.mark.parametrize("reply", [None, {"msg_id": "incoming", "msg_seq": 20001}])
async def test_delivery_mentions_target_without_visible_creator_openid(
    platform_factory, name, reply
):
    platform = platform_factory(name)
    rem = {
        **record(
            platform_name=name,
            creator_id="LONG_CREATOR_OPENID",
            creator_name="",
            target_id="LONG_TARGET_MEMBER_OPENID",
            target_name="",
        ),
        "id": 9,
    }
    await deliver(platform, rem, reply)
    sent = platform.get_client().api.post_group_message.call_args.kwargs
    token = '<qqbot-at-user id="LONG_TARGET_MEMBER_OPENID" />'
    assert sent["msg_type"] == 2 and "content" not in sent
    body = sent["markdown"]["content"]
    assert body.startswith(token + "\n\n")
    assert body.count(token) == 1
    assert "LONG_CREATOR_OPENID" not in body
    assert "@LONG_TARGET_MEMBER_OPENID" not in body
    assert "⏰ 吃饭" in body
    assert sent.get("msg_id") == (reply["msg_id"] if reply else None)


@pytest.mark.parametrize("nickname", ["", "LONG_CREATOR_OPENID"])
def test_creator_id_is_not_used_as_a_nickname(nickname):
    text = reminder_text(
        {**record(creator_id="LONG_CREATOR_OPENID", creator_name=nickname), "id": 1}
    )
    assert "LONG_CREATOR_OPENID" not in text
    assert "来自 群成员 的提醒" in text


async def test_self_reminder_has_one_native_mention_without_visible_id(platform_factory):
    rem = {
        **record(
            creator_id="MEMBER_OPENID", target_id="MEMBER_OPENID", creator_name="", target_name=""
        ),
        "id": 1,
    }
    platform = platform_factory()
    await deliver(platform, rem)
    sent = platform.get_client().api.post_group_message.call_args.kwargs["markdown"]["content"]
    assert sent == '<qqbot-at-user id="MEMBER_OPENID" />\n\n⏰ 吃饭\n\n（提醒 #1）'


@pytest.mark.parametrize("sdk_object", [False, True])
@pytest.mark.parametrize("name", ["qq_official", "qq_official_webhook"])
async def test_raw_group_member_openid_wins_over_generic_id_and_components(
    tmp_path, event_factory, sdk_object, name
):
    store = ReminderStore(tmp_path / "reminders.db")
    await store.initialize()
    raw_data = {
        "group_openid": "group_1",
        "mentions": [
            {"id": "generic_target", "member_openid": "actual_member", "username": "小明"},
            {"id": "generic_bot", "member_openid": "actual_bot", "is_you": True},
        ],
    }
    raw = SimpleNamespace(raw_data=raw_data, group_openid="group_1") if sdk_object else raw_data
    event = event_factory(platform=name, raw=raw)
    event.messages = [
        SimpleNamespace(qq="generic_target", name="", type="At"),
        SimpleNamespace(qq="generic_bot", name="", type="At"),
    ]
    text, target, nickname = await resolve_target(
        event,
        '1分钟 开会 <qqbot-at-user id="generic_target" />',
        store,
        record()["scope"],
    )
    assert text == "1分钟 开会" and target == "actual_member" and nickname == "小明"
    text, target, _ = await resolve_target(
        event, "1分钟 开会 @generic_target", store, record()["scope"]
    )
    assert target == "actual_member"


async def test_channel_keeps_channel_member_id_and_native_token(
    tmp_path, event_factory, platform_factory
):
    store = ReminderStore(tmp_path / "reminders.db")
    await store.initialize()
    event = event_factory(
        group="channel",
        raw={
            "channel_id": "channel",
            "mentions": [
                {"id": "channel_user", "member_openid": "group_user", "username": "小明"},
            ],
        },
    )
    _, target, _ = await resolve_target(event, "1分钟 开会", store, record()["scope"])
    assert target == "channel_user"
    platform = platform_factory()
    await deliver(platform, {**record(scene="channel", target_id=target), "id": 1})
    sent = platform.get_client().api.post_message.call_args.kwargs["content"]
    assert sent.startswith('<qqbot-at-user id="channel_user" />\n')


@pytest.mark.parametrize("reply", [None, {"msg_id": "incoming", "msg_seq": 20001}])
async def test_approved_template_keeps_native_mention_and_reply_context(platform_factory, reply):
    platform = platform_factory()
    await deliver(
        platform,
        {**record(), "id": 1},
        reply,
        markdown_template_id="approved_template",
        markdown_parameter="body",
    )
    sent = platform.get_client().api.post_group_message.call_args.kwargs
    assert sent["msg_type"] == 2 and "content" not in sent
    assert sent["markdown"] == {
        "custom_template_id": "approved_template",
        "params": [
            {
                "key": "body",
                "values": ['<qqbot-at-user id="user_1" />\n\n⏰ 吃饭\n\n（提醒 #1）'],
            }
        ],
    }
    assert sent.get("msg_id") == (reply["msg_id"] if reply else None)


def test_markdown_preserves_literal_content_and_keeps_mention_outside_code():
    body = reminder_markdown(
        {**record(content="```\n**测试** <qqbot-at-everyone />\n```"), "id": 7}
    )
    assert body.startswith('<qqbot-at-user id="user_1" />\n\n')
    assert "\\`\\`\\`" in body and "\\*\\*测试\\*\\*" in body
    assert "<qqbot-at-everyone" not in body
    assert "&lt;qqbot-at-everyone /&gt;" in body


def test_reported_display_name_and_reminder_number_have_no_added_backslashes():
    body = reminder_markdown({**record(creator_name="小慧(代码版)", target_id="xh"), "id": 14})
    assert body.startswith('<qqbot-at-user id="xh" />')
    assert "来自 小慧(代码版) 的提醒" in body and "（提醒 #14）" in body
    assert "\\(" not in body and "\\#" not in body


async def test_md_rejection_never_falls_back_to_literal_tag(platform_factory):
    platform = platform_factory()
    platform.get_client().api.post_group_message.side_effect = RuntimeError(
        "不允许发送原生 markdown"
    )
    with pytest.raises(MentionDeliveryError, match="official_markdown_template_id"):
        await deliver(platform, {**record(), "id": 7})
    assert platform.get_client().api.post_group_message.await_count == 1


async def test_scheduler_preserves_task_when_md_not_allowed(plugin):
    instance, platform = plugin
    platform.get_client().api.post_group_message.side_effect = RuntimeError(
        "不允许发送原生 markdown"
    )
    rem_id = await instance.store.add(record(), 10)
    await instance._check_due()
    rem = await instance.store.get(rem_id, record()["scope"])
    assert rem["status"] == "active" and rem["failure_count"] == 0
    assert "等待 Markdown 配置" in instance._format(rem)
    assert "official_markdown_template_id" in rem["waiting_reason"]
    await instance._check_due()
    assert platform.get_client().api.post_group_message.await_count == 1


async def test_scheduler_uses_configured_template(plugin):
    instance, platform = plugin
    instance.markdown_template_id = "approved_template"
    instance.markdown_parameter = "body"
    rem_id = await instance.store.add(record(), 10)
    await instance._check_due()
    sent = platform.get_client().api.post_group_message.call_args.kwargs
    assert sent["markdown"]["custom_template_id"] == "approved_template"
    assert sent["markdown"]["params"][0]["key"] == "body"
    assert (await instance.store.get(rem_id, record()["scope"]))["status"] == "done"
