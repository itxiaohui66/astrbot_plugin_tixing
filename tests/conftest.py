"""Small host boundary doubles; real time parsing and SQLite are never mocked."""

import importlib.util
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class Event:
    def __init__(
        self,
        text="/tx 1分钟 开会",
        user="user_1",
        group="group_1",
        platform="qq_official",
        raw=None,
    ):
        self.message_str = text
        self.user = user
        self.group = group
        self.platform = platform
        self.message_obj = SimpleNamespace(
            self_id="bot_id",
            raw_message=raw if raw is not None else {"group_openid": group} if group else {},
        )
        self.messages = []
        self.replies = []
        self.stopped = False
        self.admin = False
        self.unified_msg_origin = f"platform_1:GroupMessage:{user}_{group}"
        self.bot = SimpleNamespace(get_group_member_info=AsyncMock(return_value={"user_id": user}))

    def get_platform_id(self):
        return "platform_1"

    def get_platform_name(self):
        return self.platform

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.user

    def get_sender_name(self):
        return "名字_" + self.user

    def get_messages(self):
        return self.messages

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return text

    async def send(self, result):
        self.replies.append(result)

    def stop_event(self):
        self.stopped = True


@pytest.fixture
def event_factory():
    return Event


@pytest.fixture
def plugin_class(monkeypatch, tmp_path):
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("reminder-tests")
    events = types.ModuleType("astrbot.api.event")
    events.AstrMessageEvent = Event

    def decorator(*args, **kwargs):
        return lambda func: func

    events.filter = SimpleNamespace(
        command=decorator,
        on_astrbot_loaded=decorator,
        event_message_type=decorator,
        EventMessageType=SimpleNamespace(ALL="all"),
    )
    stars = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    stars.Star = Star
    stars.Context = object
    stars.StarTools = SimpleNamespace(get_data_dir=lambda name: tmp_path)
    for name, module in {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": events,
        "astrbot.api.star": stars,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "reminder_plugin_test", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    package = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, package)
    spec.loader.exec_module(package)
    main_spec = importlib.util.spec_from_file_location(
        "reminder_plugin_test.main", root / "main.py"
    )
    module = importlib.util.module_from_spec(main_spec)
    main_spec.loader.exec_module(module)
    return module.ReminderPlugin


@pytest.fixture
def platform_factory():
    def factory(name="qq_official", result=None):
        result = {"id": "sent_1"} if result is None else result
        api = SimpleNamespace(
            **{
                method: AsyncMock(return_value=result)
                for method in (
                    "post_group_message",
                    "post_c2c_message",
                    "post_message",
                    "post_dms",
                )
            }
        )
        client = SimpleNamespace(
            api=api,
            send_group_msg=AsyncMock(return_value={"message_id": 1}),
            send_private_msg=AsyncMock(return_value={"message_id": 1}),
        )
        return SimpleNamespace(
            meta=lambda: SimpleNamespace(id="platform_1", name=name),
            status=SimpleNamespace(value="running"),
            get_client=lambda: client,
        )

    return factory
