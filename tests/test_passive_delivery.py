import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from test_reminders import record

from services.delivery import deliver, incoming_reply
from services.storage import ReminderStore


@pytest.fixture
async def passive_plugin(plugin_class, platform_factory):
    platform = platform_factory()
    context = SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[platform]))
    instance = plugin_class(context, {"check_interval_seconds": 999})
    await instance.store.initialize()
    instance._ready = True
    assert instance.delivery_mode == "passive"
    yield instance, platform
    await instance.terminate()


def inbound(
    event_factory, *, message="incoming_1", group="group_1", user="user_1", age=0, text="/tx list"
):
    raw = {
        "id": message,
        "timestamp": datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat(),
    }
    if group:
        raw["group_openid"] = group
    return event_factory(text, group=group, user=user, raw=raw)


async def test_no_window_waits_without_sending_or_cancelling(passive_plugin):
    instance, platform = passive_plugin
    rem_id = await instance.store.add(record(), 10)
    for _ in range(4):
        await instance._check_due()
    rem = await instance.store.get(rem_id, record()["scope"])
    assert rem["status"] == "active" and rem["failure_count"] == 0 and rem["waiting_reason"]
    platform.get_client().api.post_group_message.assert_not_awaited()
    assert "等待可回复消息" in instance._format(rem)


@pytest.mark.parametrize("name", ["qq_official", "qq_official_webhook"])
async def test_fresh_message_delivers_pending_with_msg_id(passive_plugin, event_factory, name):
    instance, platform = passive_plugin
    rem_id = await instance.store.add(record(platform_name=name), 10)
    await instance._check_due()
    event = inbound(event_factory)
    event.platform = name
    await instance.observe_member(event)
    assert instance._wake.is_set() and not event.stopped
    await instance._check_due()
    sent = platform.get_client().api.post_group_message.call_args.kwargs
    assert sent["msg_id"] == "incoming_1" and sent["msg_seq"] > 10000
    assert (await instance.store.get(rem_id, record()["scope"]))["status"] == "done"


async def test_expired_reply_not_reused_and_group_isolated(passive_plugin, event_factory):
    instance, platform = passive_plugin
    rem_id = await instance.store.add(record(), 10)
    await instance.observe_member(inbound(event_factory, age=301))
    await instance.observe_member(inbound(event_factory, group="another_group"))
    await instance._check_due()
    platform.get_client().api.post_group_message.assert_not_awaited()
    assert (await instance.store.get(rem_id, record()["scope"]))["status"] == "active"
    await instance.observe_member(inbound(event_factory, message="fresh"))
    await instance._check_due()
    assert platform.get_client().api.post_group_message.call_args.kwargs["msg_id"] == "fresh"


async def test_reply_quota_unique_sequences_and_replay(passive_plugin, event_factory):
    instance, platform = passive_plugin
    ids = [await instance.store.add(record(), 10) for _ in range(4)]
    event = inbound(event_factory)
    await instance.observe_member(event)
    await instance._check_due()
    assert platform.get_client().api.post_group_message.await_count == 3
    sequences = [
        call.kwargs["msg_seq"]
        for call in platform.get_client().api.post_group_message.call_args_list
    ]
    assert len(set(sequences)) == 3
    await instance.observe_member(event)
    await instance._check_due()
    assert platform.get_client().api.post_group_message.await_count == 3
    assert (await instance.store.get(ids[-1], record()["scope"]))["waiting_reason"]
    await instance.observe_member(inbound(event_factory, message="new_id"))
    await instance._check_due()
    assert (await instance.store.get(ids[-1], record()["scope"]))["status"] == "done"


@pytest.mark.parametrize(
    "error", ["msg limit exceed 22009", "msg_id expired", "主动消息失败, 无权限"]
)
async def test_platform_reply_rejection_waits_for_new_message(passive_plugin, event_factory, error):
    instance, platform = passive_plugin
    api = platform.get_client().api.post_group_message
    api.side_effect = RuntimeError(error)
    rem_id = await instance.store.add(record(), 10)
    await instance.observe_member(inbound(event_factory))
    await instance._check_due()
    rem = await instance.store.get(rem_id, record()["scope"])
    assert rem["failure_count"] == 0 and rem["status"] == "active"
    assert error in rem["waiting_reason"]
    await instance._check_due()
    assert api.await_count == 1
    api.side_effect = None
    await instance.observe_member(inbound(event_factory, message="new_id"))
    await instance._check_due()
    assert (await instance.store.get(rem_id, record()["scope"]))["status"] == "done"


async def test_c2c_longer_window_and_same_user_destination(passive_plugin, event_factory):
    instance, platform = passive_plugin
    event = inbound(event_factory, group="", age=1800)
    rem_id = await instance.store.add(
        record(scene="c2c", destination="user_1", scope=instance._scope(event)), 10
    )
    await instance.observe_member(event)
    await instance._check_due()
    kwargs = platform.get_client().api.post_c2c_message.call_args.kwargs
    assert kwargs["openid"] == "user_1" and kwargs["msg_id"] == "incoming_1"
    assert (await instance.store.get(rem_id, instance._scope(event)))["status"] == "done"


async def test_retry_cancelled_legacy_failure_and_permission(passive_plugin, event_factory):
    instance, platform = passive_plugin
    rem_id = await instance.store.add(record(), 10)
    await instance.store.claim(rem_id, instance.owner, time.time())
    await instance.store.failure(rem_id, instance.owner, "主动消息失败, 无权限", 1, 0)
    denied = inbound(event_factory, user="other", text=f"/tx retry {rem_id}")
    await instance.tx(denied)
    assert "没有操作权限" in denied.replies[0]
    event = inbound(event_factory, message="retry_msg", text=f"/tx retry {rem_id}")
    await instance.tx(event)
    assert "已恢复" in event.replies[0]
    await instance._check_due()
    rem = await instance.store.get(rem_id, record()["scope"])
    assert rem["status"] == "done" and rem["failure_count"] == 0
    assert platform.get_client().api.post_group_message.call_args.kwargs["msg_id"] == "retry_msg"


async def test_context_persistence_quota_transaction_and_late_replay(tmp_path):
    first = ReminderStore(tmp_path / "db")
    await first.initialize()
    now = time.time()
    await first.remember_reply("p", "group", "g", "new", now)
    second = ReminderStore(first.path)
    await second.initialize()
    results = await asyncio.gather(
        *(s.reserve_reply("p", "group", "g", now, 240) for s in [first, second, first, second])
    )
    assert sum(r is not None for r in results) == 3
    await second.remember_reply("p", "group", "g", "old", now - 100)
    await second.remember_reply("p", "group", "g", "new", now + 100)
    assert await second.reserve_reply("p", "group", "g", now, 240) is None
    assert await second.reserve_reply("p", "group", "different_group", now, 240) is None
    assert await second.reserve_reply("different_platform", "group", "g", now, 240) is None


def test_incoming_time_validation(event_factory):
    event = inbound(event_factory, age=600)
    assert time.time() - incoming_reply(event)["received_at"] >= 599
    event.message_obj.raw_message = {"id": "x", "timestamp": "bad"}
    assert incoming_reply(event) is None
    event.message_obj.raw_message = {"id": "x", "timestamp": "2026-10-05T00:00:00"}
    assert incoming_reply(event) is None
    event.message_obj.raw_message = {"timestamp": time.time()}
    assert incoming_reply(event) is None


@pytest.mark.parametrize("scene,method", [("channel", "post_message"), ("guild_dm", "post_dms")])
async def test_guild_passive_omits_unsupported_sequence(platform_factory, scene, method):
    platform = platform_factory()
    await deliver(
        platform, {**record(scene=scene), "id": 1}, {"msg_id": "incoming", "msg_seq": 20001}
    )
    kwargs = getattr(platform.get_client().api, method).call_args.kwargs
    assert kwargs["msg_id"] == "incoming" and "msg_seq" not in kwargs


async def test_migration_from_v1_database_preserves_tasks(tmp_path):
    store = ReminderStore(tmp_path / "legacy.db")
    await store.initialize()
    rem_id = await store.add(record(), 10)
    # Remove only the added column to emulate the previously released schema.
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE reminders DROP COLUMN waiting_reason")
        db.execute("DROP TABLE reply_contexts")
    upgraded = ReminderStore(store.path)
    await upgraded.initialize()
    restored = await upgraded.get(rem_id, record()["scope"])
    assert restored["content"] == "吃饭" and restored["status"] == "active"
    assert restored["waiting_reason"] == ""


async def test_poll_wakes_on_incoming_instead_of_waiting_999_seconds(passive_plugin, event_factory):
    instance, platform = passive_plugin
    rem_id = await instance.store.add(record(), 10)
    await instance.initialize()
    try:
        await instance.observe_member(inbound(event_factory))
        for _ in range(100):
            if (await instance.store.get(rem_id, record()["scope"]))["status"] == "done":
                break
            await asyncio.sleep(0.01)
        assert (await instance.store.get(rem_id, record()["scope"]))["status"] == "done"
        assert platform.get_client().api.post_group_message.await_count == 1
    finally:
        await instance.terminate()
