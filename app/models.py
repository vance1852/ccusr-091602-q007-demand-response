"""领域模型：电价、需量窗口、设备/任务、电网指令、保护决定。

模型只携带数据并做轻量校验，业务规则集中在 services 层，便于滚动重算时
保持输入与输出的对称性（同一份仓库数据 -> 可比较的相邻版本计划）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from . import timegrid as tg

LOAD_KINDS = ("interruptible", "shiftable", "must_run", "ramping")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------- 输入模型

@dataclass
class TariffSegment:
    """分时电价段：[start_local, end_local) 每日循环，price 单位 元/kWh。"""

    start_local: str            # "08:00"
    end_local: str              # "11:30"，允许跨零点
    price: float
    label: str

    def covers_minute(self, minute_of_day: int) -> bool:
        start, end = tg.hhmm(self.start_local), tg.hhmm(self.end_local)
        if start <= end:
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end


@dataclass
class TariffSchedule:
    segments: list[TariffSegment]
    demand_charge: float = 0.0          # 元/kW，按计费窗口实际需量计
    contracted_demand_kw: float = 0.0   # 契约容量
    demand_penalty_per_kw: float = 0.0  # 超约部分每 kW 罚金（窗口级）

    def price_at(self, moment) -> float:
        moment = tg.parse(moment)
        minute = moment.hour * 60 + moment.minute
        for seg in self.segments:
            if seg.covers_minute(minute):
                return seg.price
        raise ValueError(f"时刻 {moment.isoformat()} 未被任何分时电价段覆盖")

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [asdict(s) for s in self.segments],
            "demand_charge": self.demand_charge,
            "contracted_demand_kw": self.contracted_demand_kw,
            "demand_penalty_per_kw": self.demand_penalty_per_kw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TariffSchedule":
        return cls(
            segments=[TariffSegment(**s) for s in data["segments"]],
            demand_charge=data.get("demand_charge", 0.0),
            contracted_demand_kw=data.get("contracted_demand_kw", 0.0),
            demand_penalty_per_kw=data.get("demand_penalty_per_kw", 0.0),
        )


@dataclass
class BillingWindow:
    """需量计费窗口（如 15 分钟或 30 分钟滑窗），以槽位对齐。"""

    window_id: str
    start: str
    end: str

    def __post_init__(self) -> None:
        s, e = tg.floor_slot(self.start), tg.floor_slot(self.end)
        if e <= s:
            raise ValueError(f"计费窗口 {self.window_id} 结束必须晚于开始")
        self.start, self.end = tg.iso(s), tg.iso(e)

    def slot_range(self, base) -> tuple[int, int]:
        return tg.slot_index(base, self.start), tg.slot_index(base, self.end)


@dataclass
class PowerCurvePoint:
    """相对任务起点第 offset_minutes 分钟时功率为 kw（线性爬坡由相邻点描述）。"""

    offset_minutes: int
    kw: float


@dataclass
class Task:
    """生产任务。

    - kind: interruptible 可随时切；shiftable 可整体移峰（保持功率曲线形状）；
      must_run 现场强制运行；ramping 恢复时必须先按爬坡曲线回升。
    - non_interruptible: 工艺不可中断区间（绝对时间），其内片段不可改写。
    - holding: 已进入保温阶段的任务，后续只能按保温功率运行，不可移峰。
    """

    task_id: str
    name: str
    kind: str
    power_curve: list[PowerCurvePoint]          # 已按 offset 升序
    earliest_start: str                         # 槽位对齐
    latest_finish: str                          # 槽位对齐
    duration_minutes: int
    non_interruptible: list[tuple[str, str]] = field(default_factory=list)
    forced_segments: list[tuple[str, str]] = field(default_factory=list)  # 现场强制运行区间（额定功率）
    holding_from: str | None = None             # 进入保温的绝对时刻
    holding_kw: float = 0.0                     # 保温功率
    min_run_minutes: int = 0                    # 每次启动后最少运行分钟
    max_starts: int | None = None
    starts_used: int = 0                        # 仓库中已记录的启动次数（不可改写历史）
    interlock_group: str | None = None          # 同组设备互斥（同时刻至多一个运行）
    ramp_recover_minutes: int = 0               # 恢复爬坡时长（ramping）
    history_kw: list[float] = field(default_factory=list)  # 已执行功率片段，按时序

    def __post_init__(self) -> None:
        if self.kind not in LOAD_KINDS:
            raise ValueError(f"未知负荷类型 {self.kind}")
        if self.duration_minutes % tg.SLOT_MINUTES != 0:
            raise ValueError(f"任务 {self.task_id} 时长必须对齐 {tg.SLOT_MINUTES} 分钟")
        self.earliest_start = tg.iso(tg.floor_slot(self.earliest_start))
        self.latest_finish = tg.iso(tg.floor_slot(self.latest_finish))
        self.non_interruptible = [
            (tg.iso(tg.floor_slot(a)), tg.iso(tg.floor_slot(b))) for a, b in self.non_interruptible
        ]
        self.forced_segments = [
            (tg.iso(tg.floor_slot(a)), tg.iso(tg.floor_slot(b))) for a, b in self.forced_segments
        ]
        if self.holding_from is not None:
            self.holding_from = tg.iso(tg.floor_slot(self.holding_from))
        self.power_curve = [
            p if isinstance(p, PowerCurvePoint) else PowerCurvePoint(**p)
            for p in self.power_curve
        ]
        self.power_curve.sort(key=lambda p: p.offset_minutes)

    @property
    def slots(self) -> int:
        return self.duration_minutes // tg.SLOT_MINUTES

    def curve_kw_at(self, slot_offset: int) -> float:
        """任务运行中第 slot_offset 个槽位起点（相对任务开始，单位分钟取槽起点）的功率。

        超出曲线末尾则保持末端功率。
        """
        minute = slot_offset * tg.SLOT_MINUTES
        pts = self.power_curve
        if minute <= pts[0].offset_minutes:
            return pts[0].kw
        for a, b in zip(pts, pts[1:]):
            if a.offset_minutes <= minute <= b.offset_minutes:
                span = b.offset_minutes - a.offset_minutes
                if span == 0:
                    return b.kw
                ratio = (minute - a.offset_minutes) / span
                return a.kw + (b.kw - a.kw) * ratio
        return pts[-1].kw

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["power_curve"] = [asdict(p) for p in self.power_curve]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            task_id=data["task_id"],
            name=data["name"],
            kind=data["kind"],
            power_curve=[PowerCurvePoint(**p) for p in data["power_curve"]],
            earliest_start=data["earliest_start"],
            latest_finish=data["latest_finish"],
            duration_minutes=data["duration_minutes"],
            non_interruptible=[tuple(x) for x in data.get("non_interruptible", [])],
            forced_segments=[tuple(x) for x in data.get("forced_segments", [])],
            holding_from=data.get("holding_from"),
            holding_kw=data.get("holding_kw", 0.0),
            min_run_minutes=data.get("min_run_minutes", 0),
            max_starts=data.get("max_starts"),
            starts_used=data.get("starts_used", 0),
            interlock_group=data.get("interlock_group"),
            ramp_recover_minutes=data.get("ramp_recover_minutes", 0),
            history_kw=list(data.get("history_kw", [])),
        )


@dataclass
class GridDirective:
    """电网临时下发的需量控制通知。

    notice_key 相同的重试（含同一 demand_limit/窗口）必须合并为一次决策。
    """

    notice_key: str
    start: str
    end: str
    demand_limit_kw: float
    issued_at: str
    retry_seq: int = 0                 # 相同 notice_key 的第几次重发
    source: str = "grid"

    def __post_init__(self) -> None:
        self.start = tg.iso(tg.floor_slot(self.start))
        self.end = tg.iso(tg.floor_slot(self.end))
        self.issued_at = tg.iso(self.issued_at)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GridDirective":
        return cls(**data)
