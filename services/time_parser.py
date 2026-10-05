"""Parse the old reminder syntax and the formats advertised by its help."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = {s: i for i, s in enumerate("一二三四五六日")}
WEEKDAYS.update({str(i + 1): i for i in range(7)})
WEEKDAYS["天"] = 6


@dataclass(frozen=True)
class ParsedReminder:
    when: datetime
    content: str
    repeat: str = "none"


def parse_time(text: str, now: datetime) -> ParsedReminder:
    """Always use an explicit timezone; reject past/invalid/zero times."""
    text = text.strip()
    flags = re.I | re.S
    try:
        match = re.fullmatch(r"每天\s*(\d{1,2})[:：](\d{2})\s+(.+)", text, flags)
        if match:
            hour, minute, content = match.groups()
            when = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
            if when <= now:
                when += timedelta(days=1)
            return _result(when, content, "daily")
        match = re.fullmatch(
            r"每周([一二三四五六日天1-7])\s*(\d{1,2})[:：](\d{2})\s+(.+)", text, flags
        )
        if match:
            weekday, hour, minute, content = match.groups()
            when = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
            when += timedelta(days=(WEEKDAYS[weekday] - now.weekday()) % 7)
            # The old code incorrectly skipped later times on the same weekday.
            if when <= now:
                when += timedelta(weeks=1)
            return _result(when, content, "weekly")
        match = re.fullmatch(
            r"(\d+)\s*(分钟|小时|天|minutes?|hours?|days?|min|h|d)(?:后)?\s+(.+)", text, flags
        )
        if match:
            amount, unit, content = match.groups()
            amount = int(amount)
            if amount <= 0:
                raise ValueError("提醒间隔必须大于 0。")
            unit = unit.lower()
            seconds = (
                60
                if unit in {"分钟", "minute", "minutes", "min"}
                else (3600 if unit in {"小时", "hour", "hours", "h"} else 86400)
            )
            return _result(now + timedelta(seconds=amount * seconds), content)
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2})[:：](\d{2})\s+(.+)", text, flags)
        if match:
            date, hour, minute, content = match.groups()
            when = datetime.strptime(f"{date} {hour}:{minute}", "%Y-%m-%d %H:%M").replace(
                tzinfo=now.tzinfo
            )
        else:
            match = re.fullmatch(r"(今天|明天|后天)?\s*(\d{1,2})[:：](\d{2})\s+(.+)", text, flags)
            if not match:
                raise ValueError("时间格式错误，发送 /tx help 查看全部格式。")
            day, hour, minute, content = match.groups()
            when = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
            when += timedelta(days={None: 0, "今天": 0, "明天": 1, "后天": 2}[day])
        if when <= now:
            raise ValueError("单次提醒时间已经过去，请指定未来时间。")
        return _result(when, content)
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, OverflowError) or str(exc).startswith(
            ("hour must", "minute must", "day is", "time data")
        ):
            raise ValueError("日期或时间无效，请检查日期、小时和分钟。") from exc
        raise


def _result(when, content, repeat="none"):
    content = content.strip()
    if not content:
        raise ValueError("提醒内容不能为空。")
    return ParsedReminder(when, content, repeat)


def next_occurrence(timestamp: float, repeat: str, timezone: str, now: float) -> float:
    """Skip missed cycles after downtime instead of flooding a group."""
    previous = datetime.fromtimestamp(timestamp, ZoneInfo(timezone))
    current = datetime.fromtimestamp(now, previous.tzinfo)
    step = 1 if repeat == "daily" else 7
    days = max(step, ((current.date() - previous.date()).days // step) * step)
    target = previous + timedelta(days=days)
    if target.timestamp() <= now:
        target += timedelta(days=step)
    return target.timestamp()


def describe(timestamp: float, repeat: str, timezone: str) -> str:
    when = datetime.fromtimestamp(timestamp, ZoneInfo(timezone))
    if repeat == "daily":
        return f"每天 {when:%H:%M}（下次 {when:%Y-%m-%d %H:%M}）"
    if repeat == "weekly":
        return f"每周{'一二三四五六日'[when.weekday()]} {when:%H:%M}（下次 {when:%Y-%m-%d %H:%M}）"
    return when.strftime("%Y-%m-%d %H:%M")
