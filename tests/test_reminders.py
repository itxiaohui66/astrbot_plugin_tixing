import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.delivery import deliver, route
from services.storage import ReminderStore, scope_key
from services.targets import resolve_target


def record(**changes):
    rem = dict(
        scope=scope_key("platform_1", "group_1", "user_1"),
        platform_id="platform_1",
        platform_name="qq_official",
        destination="group_1",
        scene="group",
        umo="old_umo",
        creator_id="user_1",
        creator_name="甲",
        target_id="user_1",
        target_name="甲",
        content="吃饭",
        remind_at=time.time() - 10,
        timezone="Asia/Shanghai",
        repeat_type="none",
    )
    rem.update(changes)
    return rem


async def test_atomic_limit_and_scope(tmp_path):
    store = ReminderStore(tmp_path / "test.db")
    await store.initialize()
    results = await asyncio.gather(
        *(store.add(record(), 1) for _ in range(2)), return_exceptions=True
    )
    assert sum(isinstance(r, ValueError) for r in results) == 1
    await store.add(record(scope=scope_key("platform_2", "group_1", "user_1")), 1)
    await store.add(record(scope=scope_key("platform_1", "group_2", "user_1")), 1)
    assert len(await store.list(record()["scope"])) == 1


async def test_leases_across_instances_and_restore(tmp_path):
    first = ReminderStore(tmp_path / "test.db")
    await first.initialize()
    rem_id = await first.add(record(), 10)
    second = ReminderStore(first.path)
    await second.initialize()
    now = time.time()
    claims = await asyncio.gather(first.claim(rem_id, "one", now), second.claim(rem_id, "two", now))
    assert sum(claims) == 1
    assert not await second.due(now)
    assert len(await second.due(now + 121)) == 1
    assert (await second.list(record()["scope"]))[0]["content"] == "吃饭"


async def test_cancel_authorization_and_lease(tmp_path):
    store = ReminderStore(tmp_path / "test.db")
    await store.initialize()
    rem_id = await store.add(record(), 10)
    assert not await store.cancel(record()["scope"], "other", rem_id)
    assert not await store.cancel("other_group", None, rem_id)
    assert await store.cancel(record()["scope"], "user_1", rem_id) == 1
    assert not await store.claim(rem_id, "owner", time.time())


@pytest.mark.parametrize(
    "scene,method,destination_key",
    [
        ("group", "post_group_message", "group_openid"),
        ("c2c", "post_c2c_message", "openid"),
        ("channel", "post_message", "channel_id"),
        ("guild_dm", "post_dms", "guild_id"),
    ],
)
@pytest.mark.parametrize("platform_name", ["qq_official", "qq_official_webhook"])
async def test_official_routes(platform_factory, scene, method, destination_key, platform_name):
    platform = platform_factory(platform_name)
    await deliver(platform, {**record(scene=scene, platform_name=platform_name), "id": 1})
    kwargs = getattr(platform.get_client().api, method).call_args.kwargs
    assert kwargs[destination_key] == "group_1"
    assert "msg_id" not in kwargs
    if scene == "group":
        assert kwargs["msg_type"] == 2
        assert '<qqbot-at-user id="user_1" />' in kwargs["markdown"]["content"]


async def test_empty_official_result_is_failure(platform_factory):
    platform = platform_factory()
    platform.get_client().api.post_group_message.return_value = None
    with pytest.raises(RuntimeError):
        await deliver(platform, {**record(), "id": 1})


async def test_onebot_delivery(platform_factory):
    platform = platform_factory("aiocqhttp")
    await deliver(
        platform,
        {**record(platform_name="aiocqhttp", destination="12345", target_id="67890"), "id": 1},
    )
    kwargs = platform.get_client().send_group_msg.call_args.kwargs
    assert kwargs["group_id"] == 12345
    assert kwargs["message"][0]["data"]["qq"] == "67890"


async def test_member_and_mentions(tmp_path, event_factory):
    store = ReminderStore(tmp_path / "test.db")
    await store.initialize()
    event = event_factory()
    scope = record()["scope"]
    await store.observe(scope, "opaque_OpenID", "小明")
    text, target, name = await resolve_target(event, "1小时 开会 @小明", store, scope)
    assert target == "opaque_OpenID" and name == "小明" and text == "1小时 开会"
    event.messages = [
        SimpleNamespace(qq="bot_id", name="", type="At"),
        SimpleNamespace(qq="opaque_new", name="新用户", type="At"),
    ]
    assert (await resolve_target(event, "1小时 开会", store, scope))[1] == "opaque_new"
    event.messages = []
    event.message_obj.raw_message = {
        "group_openid": "group_1",
        "mentions": [{"id": "opaque_new", "username": "新用户"}, {"id": "bot_id", "is_you": True}],
    }
    assert (
        await resolve_target(event, '1小时 <qqbot-at-user id="opaque_new" /> 开会', store, scope)
    )[1] == "opaque_new"
    event.message_obj.raw_message = {"group_openid": "group_1"}
    with pytest.raises(ValueError, match="OpenID"):
        await resolve_target(event, "1小时 开会 @123456789", store, scope)
    await store.observe("other_scope", "unknown", "跨群用户")
    with pytest.raises(ValueError):
        await resolve_target(event, "1小时 开会 @unknown", store, scope)


async def test_ambiguous_multiple_all_and_private(tmp_path, event_factory):
    store = ReminderStore(tmp_path / "test.db")
    await store.initialize()
    scope = record()["scope"]
    await store.observe(scope, "u1", "小明")
    await store.observe(scope, "u2", "小明")
    for args in ["1小时 开会 @小明", "1小时 开会 @u1 @u2", "1小时 开会 @all"]:
        with pytest.raises(ValueError):
            await resolve_target(event_factory(), args, store, scope)
    with pytest.raises(ValueError, match="私聊"):
        await resolve_target(event_factory(group=""), "1小时 开会 @u1", store, scope)


@pytest.fixture
async def plugin(plugin_class, platform_factory):
    platform = platform_factory()
    context = SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[platform]))
    instance = plugin_class(context, {"check_interval_seconds": 999})
    await instance.store.initialize()
    # Control scans explicitly in integration tests; lifecycle is tested separately.
    instance._ready = True
    instance.initialize = AsyncMock()
    yield instance, platform
    await instance.terminate()


async def test_due_success_once_and_recurring(plugin):
    instance, platform = plugin
    one_id = await instance.store.add(record(), 10)
    daily_id = await instance.store.add(
        record(repeat_type="daily", remind_at=time.time() - 86400 * 5), 10
    )
    await instance._check_due()
    assert (await instance.store.get(one_id, record()["scope"]))["status"] == "done"
    daily = await instance.store.get(daily_id, record()["scope"])
    assert daily["status"] == "active" and daily["remind_at"] > time.time()
    await instance._check_due()
    assert platform.get_client().api.post_group_message.await_count == 2


async def test_failures_retry_then_cancel_and_history(plugin, event_factory):
    instance, platform = plugin
    instance.retry_seconds = 0
    platform.get_client().api.post_group_message.side_effect = RuntimeError("服务暂时不可用")
    rem_id = await instance.store.add(record(), 10)
    for count in range(1, 4):
        await instance._check_due()
        rem = await instance.store.get(rem_id, record()["scope"])
        assert rem["failure_count"] == count
        assert rem["status"] == ("cancelled" if count == 3 else "active")
    event = event_factory("/tx history")
    await instance.tx(event)
    assert "服务暂时不可用" in event.replies[0]
    assert "已取消" in event.replies[0]


async def test_platform_unavailable_does_not_cancel(plugin):
    instance, platform = plugin
    rem_id = await instance.store.add(record(), 10)
    platform.status.value = "pending"
    await instance._check_due()
    assert (await instance.store.get(rem_id, record()["scope"]))["failure_count"] == 0
    platform.status.value = "running"
    await instance._check_due()
    assert (await instance.store.get(rem_id, record()["scope"]))["status"] == "done"


async def test_commands_scope_cancel_and_admin(plugin, event_factory):
    instance, _ = plugin
    for alias in ["tx", "提醒", "remind"]:
        event = event_factory(f"/{alias} 1小时 提醒：内容\n第二行")
        await instance.tx(event)
        assert "提醒已设置" in event.replies[0] and event.stopped
    assert len(await instance.store.list(record()["scope"])) == 3
    other = event_factory("/tx list", user="user_2")
    await instance.tx(other)
    assert "没有待提醒" in other.replies[0]
    denied = event_factory("/tx cancel 1", user="user_2")
    await instance.tx(denied)
    assert "未找到" in denied.replies[0]
    denied.admin = True
    await instance.tx(denied)
    assert "已取消 1" in denied.replies[-1]
    cross = event_factory("/tx cancel 2", group="other_group")
    cross.admin = True
    await instance.tx(cross)
    assert "未找到" in cross.replies[0]
    mine = event_factory("/tx cancel all")
    await instance.tx(mine)
    assert "已取消 2" in mine.replies[0]


async def test_private_and_member_observation(plugin, event_factory):
    instance, _ = plugin
    event = event_factory("/tx 1小时 私聊提醒", group="")
    await instance.tx(event)
    rows = await instance.store.list(instance._scope(event))
    assert rows[0]["scene"] == "c2c" and rows[0]["destination"] == "user_1"
    observe = event_factory("普通消息", user="user_2")
    await instance.observe_member(observe)
    assert not observe.stopped
    assert (await instance.store.member(record()["scope"], "user_2"))["user_id"] == "user_2"


async def test_lifecycle_restart_does_not_duplicate(plugin_class, platform_factory, event_factory):
    context = SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[platform_factory()]))
    instance = plugin_class(context, {"check_interval_seconds": 999})
    await instance.initialize()
    original = instance._scheduler
    await instance.initialize()
    await instance.on_loaded()
    assert instance._scheduler is original
    event = event_factory("/cxzd")
    await instance.restart_scheduler(event)
    assert "管理员" in event.replies[0] and instance._scheduler is original
    event.admin = True
    await instance.restart_scheduler(event)
    assert original.done() and instance._scheduler is not original
    await instance.terminate()
    assert instance._scheduler is None


async def test_unload_waits_for_delivery_persistence(plugin_class, platform_factory):
    platform = platform_factory()
    context = SimpleNamespace(platform_manager=SimpleNamespace(platform_insts=[platform]))
    instance = plugin_class(context, {"check_interval_seconds": 999})
    await instance.store.initialize()
    await instance.store.remember_reply("platform_1", "group", "group_1", "incoming", time.time())
    rem_id = await instance.store.add(record(), 10)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_send(**kwargs):
        entered.set()
        await release.wait()
        return {"id": "sent"}

    platform.get_client().api.post_group_message.side_effect = delayed_send
    await instance.initialize()
    await asyncio.wait_for(entered.wait(), timeout=2)
    unload = asyncio.create_task(instance.terminate())
    await asyncio.sleep(0)
    assert not unload.done()
    release.set()
    await unload
    restored = ReminderStore(instance.store.path)
    assert (await restored.get(rem_id, record()["scope"]))["status"] == "done"


def test_route_raw_object_and_channel(event_factory):
    assert route(event_factory(raw=SimpleNamespace(group_openid="opaque_group"))) == (
        "group",
        "opaque_group",
    )
    assert route(event_factory(group="chan", raw={"channel_id": "chan"})) == ("channel", "chan")
    assert route(event_factory(group="", raw={"channel_id": "dm", "guild_id": "guild"})) == (
        "guild_dm",
        "guild",
    )
