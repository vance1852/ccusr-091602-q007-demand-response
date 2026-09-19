"""时间网格：所有计算以 15 分钟槽位对齐，时区显式固定为 UTC+8。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
SLOT_MINUTES = 15
SLOT_HOURS = SLOT_MINUTES / 60.0
_SLOT_DELTA = timedelta(minutes=SLOT_MINUTES)


def now_utc8() -> datetime:
    return datetime.now(TZ)


def dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def parse(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(TZ) if value.tzinfo else value.replace(tzinfo=TZ)
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(TZ) if parsed.tzinfo else parsed.replace(tzinfo=TZ)


def iso(value: datetime) -> str:
    return parse(value).isoformat()


def floor_slot(value: str | datetime) -> datetime:
    moment = parse(value)
    minute = (moment.minute // SLOT_MINUTES) * SLOT_MINUTES
    return moment.replace(minute=minute, second=0, microsecond=0)


def slot_index(base: str | datetime, value: str | datetime) -> int:
    """base 必须落在槽位边界上，返回 value 对齐后相对 base 的槽号（可为负）。"""
    start = floor_slot(base)
    target = floor_slot(value)
    return int((target - start) / _SLOT_DELTA)


def slot_at(base: str | datetime, index: int) -> datetime:
    return floor_slot(base) + index * _SLOT_DELTA


def hhmm(value: str) -> int:
    """'08:30' -> 当日分钟数。"""
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def describe_slot(base, index: int) -> str:
    return iso(slot_at(base, index))
