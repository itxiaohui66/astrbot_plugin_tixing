import json
from pathlib import Path
from types import SimpleNamespace

from test_reminders import plugin as plugin
from test_reminders import record

from services.diagnostics import VERSION, diagnose, identity


async def test_diagnostic_uses_actual_payload_and_does_not_create_task(plugin, event_factory):
    instance, _ = plugin
    event = event_factory(
        "/tx diagnose 1分钟 测试 <@other_generic>",
        raw=SimpleNamespace(
            group_openid="group_1",
            mentions=[SimpleNamespace(member_openid=None)],
            raw_data={
                "content": "/tx diagnose 1分钟 测试 <@other_generic>",
                "mentions": [
                    {"id": "other_generic", "member_openid": "other_member", "username": "小明"}
                ],
                "token": "must_not_be_printed",
                "author": {"member_openid": "user_1"},
            },
        ),
    )
    await instance.tx(event)
    result, _ = json.JSONDecoder().raw_decode(event.replies[0].split("\n", 1)[1])
    assert result["插件版本"] == VERSION
    assert result["解析结果"]["对象"] == identity("other_member")
    assert not result["解析结果"]["提醒自己"]
    assert result["QQ原始mentions"][0]["id"] == identity("other_generic")
    assert result["SDK mentions"] == [{}]
    assert "other_generic" not in event.replies[0] and "other_member" not in event.replies[0]
    assert "must_not_be_printed" not in event.replies[0]
    assert "<@" not in event.replies[0]
    assert not await instance.store.list(record()["scope"])


async def test_diagnostic_explains_missing_member_id(plugin, event_factory):
    instance, _ = plugin
    event = event_factory(
        "/tx diagnose 1分钟 测试 @小明",
        raw={"group_openid": "group_1", "content": "1分钟 测试 @小明"},
    )
    result = json.loads(
        await diagnose(event, "1分钟 测试 @小明", instance.store, record()["scope"])
    )
    assert result["QQ原始mentions"] == [] and result["At消息段"] == []
    assert "还未识别" in result["解析失败"]


async def test_status_reports_installed_plugin_version(plugin, event_factory):
    instance, _ = plugin
    event = event_factory("/tx status")
    await instance.tx(event)
    assert f"插件版本：{VERSION}" in event.replies[0]
    assert f"version: {VERSION}" in (
        Path(__file__).resolve().parents[1] / "metadata.yaml"
    ).read_text(encoding="utf-8")


async def test_saved_reminder_diagnostic_compares_creator_and_target_without_sending(
    plugin, event_factory
):
    instance, platform = plugin
    number = await instance.store.add(record(target_id="other_member", target_name="xh"), 10)
    event = event_factory(f"/tx diagnose {number}")
    await instance.tx(event)
    assert f"保存对象：xh / {identity('other_member')}" in event.replies[0]
    assert f"到期 @ 使用：{identity('other_member')}" in event.replies[0]
    assert "发起人与对象 ID 相同：否" in event.replies[0]
    assert "other_member" not in event.replies[0] and "user_1" not in event.replies[0]
    assert len(await instance.store.list(record()["scope"])) == 1
    platform.get_client().api.post_group_message.assert_not_called()


async def test_reminder_diagnostic_respects_creator_and_group_access(plugin, event_factory):
    instance, _ = plugin
    number = await instance.store.add(record(creator_id="another_creator"), 10)
    event = event_factory(f"/tx diagnose {number}")
    await instance.tx(event)
    assert "没有查看权限" in event.replies[0]
    assert "保存对象" not in event.replies[0]
    event = event_factory(f"/tx diagnose {number}", group="another_group")
    event.admin = True
    await instance.tx(event)
    assert "找不到该提醒" in event.replies[0]
