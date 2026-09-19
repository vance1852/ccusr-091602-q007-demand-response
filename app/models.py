"""领域模型：制造负荷与生产承诺协同服务。

功率单位 kW，能量单位 kWh。时间轴被切分为等长槽位（slot），
槽位 0 对应 ServiceConfig.epoch（带时区）。所有计划版本、实际用能、
保护与仲裁记录均可序列化为 JSON；服务重启后已发生的用能不可改写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

EPS = 1e-6


class LoadKind(str, Enum):
    INTERRUPTIBLE = "interruptible"
    SHIFTABLE = "shiftable"
    MUST_RUN = "must_run"
    RAMPING = "ramping"


class PlanState(str, Enum):
    CALCULATED = "calculated"
    AWAITING_ARBITRATION = "awaiting_arbitration"
    APPROVED = "approved"
    ACTIVE = "active"
    CLOSED = "closed"


class ArbitrationResult(str, Enum):
    ENERGY_PRIORITY = "energy_priority"
    PRODUCTION_PROTECTED = "production_protected"
    TIME_LIMITED_EXCEPTION = "time_limited_exception"


class SegmentKind(str, Enum):
    NORMAL = "normal"
    UNINTERRUPTIBLE = "uninterruptible"
    HOLDING = "holding"


class ProtectionStatus(str, Enum):
    ACTIVE = "active"          # 生效中，调度必须遵守
    EXPIRED = "expired"        # 超过期限，自动失效
    OVERRIDDEN = "overridden"  # 仲裁：能源优先，保护被覆盖
    UPHELD = "upheld"          # 仲裁：生产受保护 / 限期例外


# ---------------------------------------------------------------------------
# 基础输入：配置、设备、任务、电价、需量窗口
# ---------------------------------------------------------------------------


@dataclass
class ServiceConfig:
    """服务配置：时间原点（含时区）、槽位时长、计划视野。"""

    epoch_iso: str               # 槽位 0 对应的 ISO 时间，需带时区，如 2026-09-19T00:00:00+08:00
    slot_minutes: int = 15
    horizon_slots: int = 96

    @property
    def epoch(self) -> datetime:
        return datetime.fromisoformat(self.epoch_iso)

    @property
    def slot_hours(self) -> float:
        return self.slot_minutes / 60.0

    @property
    def slots_per_day(self) -> int:
        return (24 * 60) // self.slot_minutes

    def slot_to_iso(self, slot: int) -> str:
        return (self.epoch + timedelta(minutes=slot * self.slot_minutes)).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch_iso": self.epoch_iso,
            "slot_minutes": self.slot_minutes,
            "horizon_slots": self.horizon_slots,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServiceConfig":
        return cls(**data)


@dataclass
class Device:
    """用能设备。

    interlock_group: 同组设备同一槽位只允许一台带载（设备互锁）。
    max_starts_per_day: 每日启停次数上限（按 epoch 对齐的自然日计）。
    ramp_slots / ramp_power_kw: 恢复爬坡限制——设备从停机恢复带载前，
        需要 ramp_slots 个槽位的暖机爬坡，功率为 ramp_power_kw。
    """

    id: str
    name: str
    kind: LoadKind
    max_power_kw: float
    interlock_group: Optional[str] = None
    max_starts_per_day: Optional[int] = None
    ramp_slots: int = 0
    ramp_power_kw: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind.value,
            "max_power_kw": self.max_power_kw,
            "interlock_group": self.interlock_group,
            "max_starts_per_day": self.max_starts_per_day,
            "ramp_slots": self.ramp_slots,
            "ramp_power_kw": self.ramp_power_kw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Device":
        data = dict(data)
        data["kind"] = LoadKind(data["kind"])
        return cls(**data)


@dataclass
class SegmentSpec:
    """任务功率曲线的一段。kind 标记工艺不可中断区间与保温阶段。"""

    duration_slots: int
    power_kw: float
    kind: SegmentKind = SegmentKind.NORMAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_slots": self.duration_slots,
            "power_kw": self.power_kw,
            "kind": self.kind.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SegmentSpec":
        data = dict(data)
        data["kind"] = SegmentKind(data["kind"])
        return cls(**data)


@dataclass
class TaskSpec:
    """生产任务：挂在设备上的一条功率曲线及其完工/启停约束。"""

    id: str
    device_id: str
    name: str
    segments: list[SegmentSpec]
    earliest_start: int          # 最早可启动槽位
    latest_finish: int           # 最迟完工槽位
    max_pauses: int = 0          # 允许的中断（暂停）次数
    priority: int = 0            # 数值越大越优先安排

    @property
    def total_slots(self) -> int:
        return sum(s.duration_slots for s in self.segments)

    def segment_at(self, progress: int) -> tuple[int, SegmentSpec, int]:
        """返回第 progress 个执行槽位所在的 (段索引, 段, 段内偏移)。"""
        acc = 0
        for idx, seg in enumerate(self.segments):
            if progress < acc + seg.duration_slots:
                return idx, seg, progress - acc
            acc += seg.duration_slots
        return len(self.segments) - 1, self.segments[-1], self.segments[-1].duration_slots

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "name": self.name,
            "segments": [s.to_dict() for s in self.segments],
            "earliest_start": self.earliest_start,
            "latest_finish": self.latest_finish,
            "max_pauses": self.max_pauses,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        data = dict(data)
        data["segments"] = [SegmentSpec.from_dict(s) for s in data["segments"]]
        return cls(**data)


@dataclass
class TariffPeriod:
    start_slot: int
    end_slot: int
    price_per_kwh: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_slot": self.start_slot,
            "end_slot": self.end_slot,
            "price_per_kwh": self.price_per_kwh,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TariffPeriod":
        return cls(**data)


@dataclass
class Tariff:
    """分时电价。未覆盖的槽位使用 default_price。"""

    periods: list[TariffPeriod]
    default_price: float = 0.0

    def price_at(self, slot: int) -> float:
        for p in self.periods:
            if p.start_slot <= slot < p.end_slot:
                return p.price_per_kwh
        return self.default_price

    def to_dict(self) -> dict[str, Any]:
        return {
            "periods": [p.to_dict() for p in self.periods],
            "default_price": self.default_price,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Tariff":
        return cls(
            periods=[TariffPeriod.from_dict(p) for p in data["periods"]],
            default_price=data.get("default_price", 0.0),
        )


@dataclass
class DemandWindow:
    """计费需量窗口：明确的起止边界、契约容量与超量罚金单价。"""

    id: str
    start_slot: int
    end_slot: int
    contract_kw: float
    penalty_per_kw: float        # 峰值超过契约容量时，每 kW 的罚金

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_slot": self.start_slot,
            "end_slot": self.end_slot,
            "contract_kw": self.contract_kw,
            "penalty_per_kw": self.penalty_per_kw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DemandWindow":
        return cls(**data)


# ---------------------------------------------------------------------------
# 事件：电网指令、现场强制运行、预测偏差、生产保护
# ---------------------------------------------------------------------------


@dataclass
class GridNotice:
    """电网临时需量控制通知。id 为去重键：相同通知重试合并为一次决策。"""

    id: str
    start_slot: int
    end_slot: int
    max_demand_kw: float
    received_slot: int = 0

    def payload_key(self) -> tuple[Any, ...]:
        """用于幂等判定的载荷（接收时刻不参与）。"""
        return (self.start_slot, self.end_slot, round(self.max_demand_kw, 6))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_slot": self.start_slot,
            "end_slot": self.end_slot,
            "max_demand_kw": self.max_demand_kw,
            "received_slot": self.received_slot,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GridNotice":
        return cls(**data)


@dataclass
class ForcedRun:
    """现场强制运行：任务必须从 start_slot 起连续运行完剩余曲线。"""

    task_id: str
    start_slot: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "start_slot": self.start_slot,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ForcedRun":
        return cls(**data)


@dataclass
class ForecastDeviation:
    """预测偏差：base_load 修正基荷 forecast；task_power 按比例修正任务剩余功率。"""

    kind: str                              # "base_load" | "task_power"
    start_slot: int = 0
    end_slot: int = 0                      # base_load 专用
    delta_kw: float = 0.0                  # base_load 专用
    task_id: Optional[str] = None          # task_power 专用
    scale: float = 1.0                     # task_power 专用

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start_slot": self.start_slot,
            "end_slot": self.end_slot,
            "delta_kw": self.delta_kw,
            "task_id": self.task_id,
            "scale": self.scale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ForecastDeviation":
        return cls(**data)


@dataclass
class Protection:
    """生产经理提出的有期限保护：until_slot 之前任务按基线安排不可被改写。

    baseline: 保护生效时任务的基线安排 {slot: {"kw", "seg_idx"}}。
    scope: "window" 表示仅在期限内受保护；"full" 表示仲裁后整段剩余曲线受保护。
    """

    id: str
    task_id: str
    until_slot: int
    reason: str
    created_slot: int
    created_by: str
    baseline: dict[int, dict[str, Any]] = field(default_factory=dict)
    status: ProtectionStatus = ProtectionStatus.ACTIVE
    scope: str = "window"
    arbitration_result: Optional[str] = None

    def locks_slot(self, slot: int, now_slot: int) -> bool:
        if self.status not in (ProtectionStatus.ACTIVE, ProtectionStatus.UPHELD):
            return False
        if slot < now_slot:
            return False
        if self.scope == "window" and slot >= self.until_slot:
            return False
        return slot in self.baseline

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "until_slot": self.until_slot,
            "reason": self.reason,
            "created_slot": self.created_slot,
            "created_by": self.created_by,
            "baseline": {str(s): v for s, v in self.baseline.items()},
            "status": self.status.value,
            "scope": self.scope,
            "arbitration_result": self.arbitration_result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Protection":
        data = dict(data)
        data["baseline"] = {int(s): v for s, v in data.get("baseline", {}).items()}
        data["status"] = ProtectionStatus(data["status"])
        return cls(**data)


# ---------------------------------------------------------------------------
# 计划版本与分析结果
# ---------------------------------------------------------------------------


@dataclass
class ScheduledSlot:
    """计划/执行中一个槽位的任务功率。source 标记其来源与不可改性。"""

    kw: float
    seg_idx: int
    source: str = "planned"  # executed | holding_lock | protection_lock | forced | planned

    def to_dict(self) -> dict[str, Any]:
        return {"kw": self.kw, "seg_idx": self.seg_idx, "source": self.source}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledSlot":
        return cls(**data)


@dataclass
class UnmetConstraint:
    """未满足约束记录。"""

    constraint: str            # demand_cap | latest_finish | max_pauses | max_starts | interlock | ramp | interrupted_uninterruptible | planned_not_executed | placement_infeasible
    subject: str               # 关联对象（窗口/任务/设备 id）
    detail: str
    slot: Optional[int] = None
    window_id: Optional[str] = None
    task_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint": self.constraint,
            "subject": self.subject,
            "detail": self.detail,
            "slot": self.slot,
            "window_id": self.window_id,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UnmetConstraint":
        return cls(**data)


@dataclass
class Sacrifice:
    """削减选择的代价：为满足需量上限，某任务被移动/中断所牺牲的东西。"""

    kind: str                  # task_moved_for_cap
    task_id: str
    detail: str
    window_id: Optional[str] = None
    cost_delta: float = 0.0    # 由此增加的电费（元）
    extra_pauses: int = 0
    old_start: Optional[int] = None
    new_start: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "task_id": self.task_id,
            "detail": self.detail,
            "window_id": self.window_id,
            "cost_delta": self.cost_delta,
            "extra_pauses": self.extra_pauses,
            "old_start": self.old_start,
            "new_start": self.new_start,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Sacrifice":
        return cls(**data)


@dataclass
class WindowAnalysis:
    """单个计费窗口内的计划视图（已发生槽位取实际值，未来槽位取计划值）。"""

    window_id: str
    start_slot: int
    end_slot: int
    contract_kw: float
    caps: list[dict[str, Any]]          # 窗口内生效的电网指令上限
    peak_kw: float
    peak_slot: int
    contributions: list[dict[str, Any]] # 峰值槽位的功率来源 [{source, kw}] 降序
    energy_kwh: float
    energy_cost: float
    demand_penalty: float
    cap_exceeded: bool
    over_contract: bool

    @property
    def total_cost(self) -> float:
        return self.energy_cost + self.demand_penalty

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "start_slot": self.start_slot,
            "end_slot": self.end_slot,
            "contract_kw": self.contract_kw,
            "caps": self.caps,
            "peak_kw": self.peak_kw,
            "peak_slot": self.peak_slot,
            "contributions": self.contributions,
            "energy_kwh": self.energy_kwh,
            "energy_cost": self.energy_cost,
            "demand_penalty": self.demand_penalty,
            "cap_exceeded": self.cap_exceeded,
            "over_contract": self.over_contract,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowAnalysis":
        return cls(**data)


@dataclass
class PlanVersion:
    """一版滚动计划。每次事件（电网指令/强制运行/预测偏差/仲裁）产生一版，
    与上一版可比较；已执行槽位以 source=executed 原样保留。"""

    id: str
    version_no: int
    parent_id: Optional[str]
    trigger: dict[str, Any]              # {type, ref, detail?}
    created_slot: int
    state: PlanState
    assignments: dict[str, dict[int, ScheduledSlot]]   # task_id -> slot -> ScheduledSlot
    warmups: dict[str, dict[int, float]]               # device_id -> slot -> kw
    site_kw: list[float]                               # 全视野每槽位园区总功率
    windows: dict[str, WindowAnalysis]
    total_energy_cost: float
    total_demand_penalty: float
    total_cost: float
    sacrifices: list[Sacrifice] = field(default_factory=list)
    unmet: list[UnmetConstraint] = field(default_factory=list)
    diff_summary: dict[str, Any] = field(default_factory=dict)
    closed_reason: Optional[str] = None

    def task_slots(self, task_id: str) -> dict[int, ScheduledSlot]:
        return self.assignments.get(task_id, {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version_no": self.version_no,
            "parent_id": self.parent_id,
            "trigger": self.trigger,
            "created_slot": self.created_slot,
            "state": self.state.value,
            "assignments": {
                tid: {str(s): slot.to_dict() for s, slot in slots.items()}
                for tid, slots in self.assignments.items()
            },
            "warmups": {d: {str(s): kw for s, kw in slots.items()} for d, slots in self.warmups.items()},
            "site_kw": self.site_kw,
            "windows": {w: a.to_dict() for w, a in self.windows.items()},
            "total_energy_cost": self.total_energy_cost,
            "total_demand_penalty": self.total_demand_penalty,
            "total_cost": self.total_cost,
            "sacrifices": [s.to_dict() for s in self.sacrifices],
            "unmet": [u.to_dict() for u in self.unmet],
            "diff_summary": self.diff_summary,
            "closed_reason": self.closed_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanVersion":
        data = dict(data)
        data["state"] = PlanState(data["state"])
        data["assignments"] = {
            tid: {int(s): ScheduledSlot.from_dict(v) for s, v in slots.items()}
            for tid, slots in data["assignments"].items()
        }
        data["warmups"] = {d: {int(s): kw for s, kw in slots.items()} for d, slots in data["warmups"].items()}
        data["windows"] = {w: WindowAnalysis.from_dict(a) for w, a in data["windows"].items()}
        data["sacrifices"] = [Sacrifice.from_dict(s) for s in data["sacrifices"]]
        data["unmet"] = [UnmetConstraint.from_dict(u) for u in data["unmet"]]
        return cls(**data)


@dataclass
class ArbitrationCase:
    """批准与保护冲突时产生的待仲裁事项。"""

    id: str
    plan_id: str
    protection_id: str
    task_id: str
    reason: str
    created_slot: int
    status: str = "pending"              # pending | resolved
    result: Optional[str] = None         # ArbitrationResult.value
    resolved_slot: Optional[int] = None
    resolved_by: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "protection_id": self.protection_id,
            "task_id": self.task_id,
            "reason": self.reason,
            "created_slot": self.created_slot,
            "status": self.status,
            "result": self.result,
            "resolved_slot": self.resolved_slot,
            "resolved_by": self.resolved_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArbitrationCase":
        return cls(**data)
