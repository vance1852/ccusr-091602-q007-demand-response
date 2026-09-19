"""验收场景工厂：分时电价、30 分钟需量计费窗口、热处理炉/空压机等设备。"""

from __future__ import annotations

from .models import (
    BillingWindow,
    GridDirective,
    PowerCurvePoint,
    TariffSchedule,
    TariffSegment,
    Task,
)
from .repository import Warehouse
from .service import CoordinationService
from . import timegrid as tg


def build_tariff() -> TariffSchedule:
    return TariffSchedule(
        segments=[
            TariffSegment("00:00", "08:00", 0.35, "谷"),
            TariffSegment("08:00", "11:30", 0.85, "峰"),
            TariffSegment("11:30", "13:30", 0.60, "平"),
            TariffSegment("13:30", "19:00", 0.95, "尖"),
            TariffSegment("19:00", "22:00", 0.85, "峰"),
            TariffSegment("22:00", "24:00", 0.45, "谷"),
        ],
        demand_charge=48.0,
        contracted_demand_kw=900.0,
        demand_penalty_per_kw=120.0,
    )


def build_windows(day0: str) -> list[BillingWindow]:
    """从 day0 08:00 起，每 30 分钟一个需量计费窗口，共 9 个（覆盖 08:00-12:30）。"""
    base = tg.parse(day0).replace(hour=8, minute=0)
    windows = []
    for k in range(9):
        s = base + tg._SLOT_DELTA * (2 * k)
        e = s + tg._SLOT_DELTA * 2
        windows.append(BillingWindow(f"W{k+1:02d}", tg.iso(s), tg.iso(e)))
    return windows


def build_tasks(day0: str) -> list[Task]:
    t0800 = tg.iso(tg.parse(day0).replace(hour=8, minute=0))
    t0830 = tg.iso(tg.parse(day0).replace(hour=8, minute=30))
    t0845 = tg.iso(tg.parse(day0).replace(hour=8, minute=45))
    t0900 = tg.iso(tg.parse(day0).replace(hour=9, minute=0))
    t0930 = tg.iso(tg.parse(day0).replace(hour=9, minute=30))
    t1000 = tg.iso(tg.parse(day0).replace(hour=10, minute=0))
    t1100 = tg.iso(tg.parse(day0).replace(hour=11, minute=0))
    t1200 = tg.iso(tg.parse(day0).replace(hour=12, minute=0))
    t1230 = tg.iso(tg.parse(day0).replace(hour=12, minute=30))
    return [
        # 热处理炉 A：08:00-09:00 以 450kW 升温（工艺不可中断），09:00 进入保温 80kW
        Task(
            task_id="furnace_a", name="1号热处理炉", kind="shiftable",
            power_curve=[PowerCurvePoint(0, 450), PowerCurvePoint(60, 450)],
            earliest_start=t0800, latest_finish=t1100, duration_minutes=60,
            non_interruptible=[(t0800, t0900)],
            holding_from=t0900, holding_kw=80,
            max_starts=2,
        ),
        # 空压机 1：220kW，可中断，08:00-10:15 运行，最少运行 30 分钟，最多启停 3 次
        Task(
            task_id="compressor_1", name="1号空压机", kind="interruptible",
            power_curve=[PowerCurvePoint(0, 220)],
            earliest_start=t0800, latest_finish=t1230, duration_minutes=135,
            min_run_minutes=30, max_starts=3, starts_used=0,
            interlock_group="air",
        ),
        # 空压机 2：180kW，可中断，与 1 号互锁，11:15 接班至 12:30
        Task(
            task_id="compressor_2", name="2号空压机", kind="interruptible",
            power_curve=[PowerCurvePoint(0, 180)],
            earliest_start=tg.iso(tg.parse(day0).replace(hour=11, minute=15)),
            latest_finish=t1230, duration_minutes=75,
            min_run_minutes=30, max_starts=3, starts_used=0,
            interlock_group="air",
        ),
        # 压铸机：可移峰 260kW，2 小时，最迟 12:30
        Task(
            task_id="die_cast", name="压铸线", kind="shiftable",
            power_curve=[PowerCurvePoint(0, 260), PowerCurvePoint(60, 260)],
            earliest_start=t0830, latest_finish=t1230, duration_minutes=120,
            interlock_group=None,
        ),
        # 循环风机：ramping，120kW，恢复爬坡 30 分钟
        Task(
            task_id="fan_ramp", name="循环风机", kind="ramping",
            power_curve=[PowerCurvePoint(0, 120)],
            earliest_start=t0845, latest_finish=t1230, duration_minutes=225,
            ramp_recover_minutes=30, min_run_minutes=15, max_starts=4,
        ),
        # 照明与基础负荷：must_run 130kW
        Task(
            task_id="base_load", name="照明与基础负荷", kind="must_run",
            power_curve=[PowerCurvePoint(0, 130)],
            earliest_start=t0800, latest_finish=t1230, duration_minutes=270,
        ),
    ]


def build_directive(day0: str, seq: int = 0) -> GridDirective:
    start = tg.iso(tg.parse(day0).replace(hour=9, minute=0))
    end = tg.iso(tg.parse(day0).replace(hour=10, minute=30))
    return GridDirective(
        notice_key="GRID-DR-2026-0919-01",
        start=start, end=end, demand_limit_kw=700.0,
        issued_at=tg.iso(tg.parse(day0).replace(hour=8, minute=50)),
        retry_seq=seq,
    )


def build_service(path: str | None = None, day0: str = "2026-09-19T08:00"):
    wh = Warehouse(path)
    wh.configure(
        tariff=build_tariff(),
        windows=build_windows(day0),
        horizon_start=tg.iso(tg.parse(day0).replace(hour=8, minute=0)),
        horizon_slots=18,                # 08:00 ~ 12:30
        clock_start=day0,
    )
    for task in build_tasks(day0):
        wh.upsert_task(task)
    return CoordinationService(wh)
