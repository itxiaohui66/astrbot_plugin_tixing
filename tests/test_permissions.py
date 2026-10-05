import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_reminders import record

from services.storage import ReminderStore


@pytest.fixture
async def official_plugin(plugin_class, platform_factory):
    platform = platform_factory()
    instance = plugin_class(
        SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[platform])),
        {"check_interval_seconds": 999, "official_delivery_mode": "passive"},
    )
    await instance.store.initialize()
    instance._ready = True
    await instance._attach_platforms()
    instance.initialize = AsyncMock()
    yield instance, platform
    await instance.terminate()


def lifecycle(event_id="join_1", timestamp=None, destination="group_1"):
    return SimpleNamespace(
        group_openid=destination,
        event_id=event_id,
        timestamp=time.time() if timestamp is None else timestamp,
    )


async def test_default_and_upgrade_use_proactive_without_reply_context(official_plugin):
    instance, platform = official_plugin
    assert instance.delivery_mode == "proactive"
    rem_id = await instance.store.add(record(), 10)
    await instance._check_due()
    sent = platform.get_client().api.post_group_message.call_args.kwargs
    assert "msg_id" not in sent and "event_id" not in sent
    assert (await instance.store.get(rem_id, record()["scope"]))["status"] == "done"


async def test_first_command_guidance_once_per_group_persisted(official_plugin, event_factory):
    instance, _ = official_plugin
    events = [event_factory("/tx help"), event_factory("/tx list", user="other")]
    for event in events:
        await instance.tx(event)
    assert "确认已开启" in events[0].replies[0]
    assert "确认已开启" not in events[1].replies[0]
    other = event_factory("/tx list", group="other")
    await instance.tx(other)
    assert "确认已开启" in other.replies[0]
    reopened = ReminderStore(instance.store.path)
    assert not await reopened.claim_notice("platform_1", "group_1")
    assert await reopened.claim_notice("platform_2", "group_1")


@pytest.mark.parametrize("name", ["qq_official", "qq_official_webhook"])
async def test_join_uses_event_reply_and_deduplicates(official_plugin, event_factory, name):
    instance, platform = official_plugin
    platform.meta = lambda: SimpleNamespace(id="platform_1", name=name)
    client = platform.get_client()
    event = lifecycle()
    await client.on_group_add_robot(event)
    await client.on_group_add_robot(event)
    assert client.api.post_group_message.await_count == 1
    sent = client.api.post_group_message.call_args.kwargs
    assert sent["event_id"] == "join_1" and "msg_id" not in sent
    assert "群主或管理员" in sent["content"]
    command = event_factory("/tx list")
    await instance.tx(command)
    assert "确认已开启" not in command.replies[0]


async def test_failed_join_falls_back_to_first_command(official_plugin, event_factory):
    instance, platform = official_plugin
    platform.get_client().api.post_group_message.side_effect = RuntimeError("event expired")
    await platform.get_client().on_group_add_robot(lifecycle())
    command = event_factory("/tx list")
    await instance.tx(command)
    assert "确认已开启" in command.replies[0]


async def test_failed_command_notice_is_not_marked_sent(official_plugin, event_factory):
    instance, _ = official_plugin
    command = event_factory()
    command.send = AsyncMock(side_effect=RuntimeError("network error"))
    with pytest.raises(RuntimeError):
        await instance._reply(command, "test")
    assert (await instance.store.permission("platform_1", "group_1"))["notified"] == 0
    retry = event_factory("/tx list")
    await instance.tx(retry)
    assert "确认已开启" in retry.replies[0]


async def test_granted_state_skips_first_use_notice_and_stale_join(official_plugin, event_factory):
    instance, platform = official_plugin
    now = time.time()
    await platform.get_client().on_group_msg_receive(lifecycle("receive", now))
    await platform.get_client().on_group_add_robot(lifecycle("old_join", now - 10))
    command = event_factory("/tx list")
    await instance.tx(command)
    assert "确认已开启" not in command.replies[0]
    platform.get_client().api.post_group_message.assert_not_awaited()
    assert (await instance.store.permission("platform_1", "group_1"))["state"] == "granted"


async def test_permission_error_preserved_then_receive_event_wakes_delivery(official_plugin):
    instance, platform = official_plugin
    api = platform.get_client().api
    api.post_group_message.side_effect = RuntimeError("主动消息失败, 无权限")
    rem_id = await instance.store.add(record(), 10)
    await instance._check_due()
    for _ in range(4):
        await instance._check_due()
    rem = await instance.store.get(rem_id, record()["scope"])
    assert rem["status"] == "active" and rem["failure_count"] == 0
    assert "等待主动消息权限" in instance._format(rem)
    assert rem["retry_at"] > time.time() + 290
    assert api.post_group_message.await_count == 1
    api.post_group_message.side_effect = None
    await platform.get_client().on_group_msg_receive(lifecycle("allow"))
    assert instance._wake.is_set()
    await instance._check_due()
    assert (await instance.store.get(rem_id, record()["scope"]))["status"] == "done"


async def test_reject_holds_tasks_and_permission_check_releases(official_plugin, event_factory):
    instance, platform = official_plugin
    await platform.get_client().on_group_msg_reject(lifecycle("reject"))
    rem_id = await instance.store.add(record(), 10)
    await instance._check_due()
    platform.get_client().api.post_group_message.assert_not_awaited()
    assert (await instance.store.get(rem_id, record()["scope"]))["status"] == "active"
    command = event_factory("/tx permission check")
    await instance.tx(command)
    assert command.stopped
    assert (await instance.store.permission("platform_1", "group_1"))["state"] == "granted"
    sent = platform.get_client().api.post_group_message.call_args.kwargs
    assert "msg_id" not in sent and "event_id" not in sent
    await instance._check_due()
    assert (await instance.store.get(rem_id, record()["scope"]))["status"] == "done"


async def test_permission_check_failure_is_a_passive_reply(official_plugin, event_factory):
    instance, platform = official_plugin
    platform.get_client().api.post_group_message.side_effect = RuntimeError("主动消息失败, 无权限")
    command = event_factory("/tx permission check")
    await instance.tx(command)
    assert "验证失败" in command.replies[0]
    assert command.replies[0].count("确认已开启") == 1
    assert (await instance.store.permission("platform_1", "group_1"))["state"] == "error"


async def test_check_without_message_id_is_not_success(official_plugin, event_factory):
    instance, platform = official_plugin
    platform.get_client().api.post_group_message.return_value = {}
    command = event_factory("/tx permission check")
    await instance.tx(command)
    assert "未返回消息 ID" in command.replies[0]
    assert (await instance.store.permission("platform_1", "group_1"))["state"] != "granted"


async def test_member_cannot_set_authorized_state_by_text(official_plugin, event_factory):
    instance, _ = official_plugin
    command = event_factory("/tx permission 已开启")
    await instance.tx(command)
    assert "用法" in command.replies[0]
    assert (await instance.store.permission("platform_1", "group_1"))["state"] == "unknown"


async def test_bridge_preserves_callbacks_and_overlapping_unload(official_plugin):
    instance, platform = official_plugin
    from reminder_plugin_test.services.permissions import PermissionBridge

    await instance.terminate()
    client = platform.get_client()
    original = AsyncMock()
    client.on_group_add_robot = original
    handlers = [AsyncMock(), AsyncMock()]
    first = PermissionBridge(platform, handlers[0])
    second = PermissionBridge(platform, handlers[1])
    first.install()
    second.install()
    await first.close()
    await client.on_group_add_robot(lifecycle())
    handlers[0].assert_not_awaited()
    handlers[1].assert_awaited_once()
    original.assert_awaited_once()
    await second.close()
    assert client.on_group_add_robot is original
    assert not hasattr(client, "on_group_msg_receive")


async def test_bridge_close_finishes_pending_event(official_plugin):
    instance, platform = official_plugin
    client = platform.get_client()
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await release.wait()
        return {"id": "sent"}

    client.api.post_group_message.side_effect = send
    event_task = asyncio.create_task(client.on_group_add_robot(lifecycle()))
    await asyncio.wait_for(entered.wait(), 1)
    closing = asyncio.create_task(instance.terminate())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(closing, event_task), 1)
    assert (await instance.store.permission("platform_1", "group_1"))["notified"] == 1


async def test_concurrent_first_commands_emit_one_notice(official_plugin, event_factory):
    instance, _ = official_plugin
    commands = [event_factory("/tx list", user=f"user_{i}") for i in range(8)]
    await asyncio.gather(*(instance.tx(event) for event in commands))
    assert sum("确认已开启" in event.replies[0] for event in commands) == 1


async def test_repeated_permission_failure_does_not_repeat_notice(official_plugin, event_factory):
    instance, platform = official_plugin
    platform.get_client().api.post_group_message.side_effect = RuntimeError("主动消息失败, 无权限")
    rem_id = await instance.store.add(record(), 10)
    await instance._check_due()
    first = event_factory("/tx list")
    await instance.tx(first)
    assert "确认已开启" in first.replies[0]
    await instance.store.resume(record()["scope"], "user_1", rem_id, 10)
    await instance._check_due()
    second = event_factory("/tx list")
    await instance.tx(second)
    assert "确认已开启" not in second.replies[0]
    rem = await instance.store.get(rem_id, record()["scope"])
    assert rem["status"] == "active" and rem["failure_count"] == 0


async def test_del_and_rejoin_reset_guidance(official_plugin, event_factory):
    instance, platform = official_plugin
    client = platform.get_client()
    stamp = time.time()
    await client.on_group_add_robot(lifecycle("join", stamp))
    await client.on_group_del_robot(lifecycle("del", stamp + 1))
    rem_id = await instance.store.add(record(), 10)
    await instance._check_due()
    assert client.api.post_group_message.await_count == 1
    assert (await instance.store.get(rem_id, record()["scope"]))["status"] == "active"
    await client.on_group_add_robot(lifecycle("rejoin", stamp + 2))
    assert client.api.post_group_message.await_count == 2


async def test_new_and_removed_platforms_attach_and_restore(official_plugin, platform_factory):
    instance, _ = official_plugin
    newcomer = platform_factory("qq_official_webhook")
    original = AsyncMock()
    newcomer.get_client().on_group_msg_receive = original
    instance.context.platform_manager.platform_insts.append(newcomer)
    await instance._attach_platforms()
    await newcomer.get_client().on_group_msg_receive(lifecycle("receive"))
    original.assert_awaited_once()
    instance.context.platform_manager.platform_insts.remove(newcomer)
    await instance._attach_platforms()
    assert newcomer.get_client().on_group_msg_receive is original
    assert not hasattr(newcomer.get_client(), "on_group_add_robot")
