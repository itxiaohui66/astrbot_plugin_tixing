from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from services.time_parser import next_occurrence, parse_time

NOW = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))  # Monday


@pytest.mark.parametrize(
    "text,delta",
    [
        ("30分钟 内容", timedelta(minutes=30)),
        ("2小时后 内容", timedelta(hours=2)),
        ("3天 内容", timedelta(days=3)),
        ("30min 内容", timedelta(minutes=30)),
        ("2H 内容", timedelta(hours=2)),
        ("1day 内容", timedelta(days=1)),
        ("1minutes 内容", timedelta(minutes=1)),
        ("2 hours 内容", timedelta(hours=2)),
        ("1d 内容", timedelta(days=1)),
    ],
)
def test_relative(text, delta):
    parsed = parse_time(text, NOW)
    assert parsed.when == NOW + delta
    assert parsed.content == "内容"
    assert parsed.repeat == "none"


@pytest.mark.parametrize(
    "text,expected,repeat",
    [
        ("每天9:00 内容", "2026-10-05 09:00", "daily"),
        ("每天 8:00 内容", "2026-10-06 08:00", "daily"),
        ("每周一 9:00 内容", "2026-10-05 09:00", "weekly"),
        ("每周1 7:00 内容", "2026-10-12 07:00", "weekly"),
        ("每周日 9:00 内容", "2026-10-11 09:00", "weekly"),
        ("每周天9:00 内容", "2026-10-11 09:00", "weekly"),
        ("每周7 9:00 内容", "2026-10-11 09:00", "weekly"),
        ("明天10:00 内容", "2026-10-06 10:00", "none"),
        ("后天 10:00 内容", "2026-10-07 10:00", "none"),
        ("今天 10:00 内容", "2026-10-05 10:00", "none"),
        ("10:00 内容", "2026-10-05 10:00", "none"),
        ("2026-12-31 20:00 内容", "2026-12-31 20:00", "none"),
    ],
)
def test_clock(text, expected, repeat):
    parsed = parse_time(text, NOW)
    assert parsed.when.strftime("%Y-%m-%d %H:%M") == expected
    assert parsed.repeat == repeat


@pytest.mark.parametrize(
    "text",
    [
        "0分钟 内容",
        "-1小时 内容",
        "每天25:00 内容",
        "每周一9:60 内容",
        "每周八9:00 内容",
        "7:00 过去",
        "今天8:00 过去",
        "2026-02-30 9:00 无效",
        "1分钟",
        "1分钟   ",
        "999999999999999999天 内容",
    ],
)
def test_reject_invalid(text):
    with pytest.raises(ValueError):
        parse_time(text, NOW)


def test_content_keeps_newlines_and_colons():
    assert parse_time("每天9：00 提醒：吃饭\n第二行", NOW).content == "提醒：吃饭\n第二行"


@pytest.mark.parametrize(
    "repeat,old,now,expected",
    [
        ("daily", "2026-10-01 09:00", "2026-10-05 12:00", "2026-10-06 09:00"),
        ("daily", "2026-10-01 09:00", "2026-10-05 08:00", "2026-10-05 09:00"),
        ("weekly", "2026-09-07 09:00", "2026-10-05 12:00", "2026-10-12 09:00"),
        ("weekly", "2026-09-07 09:00", "2026-10-05 08:00", "2026-10-05 09:00"),
    ],
)
def test_skip_missed_cycles(repeat, old, now, expected):
    def stamp(value):
        return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=NOW.tzinfo).timestamp()

    assert next_occurrence(stamp(old), repeat, "Asia/Shanghai", stamp(now)) == stamp(expected)
