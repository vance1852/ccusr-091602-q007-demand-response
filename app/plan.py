"""计划与决定的输出模型。

每个 Plan 是一个不可变版本：修订触发新版本，相邻版本可通过 diff 比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from . import timegrid as tg


@dataclass
class Sacrifice:
    """一次削峰/移峰动作牺牲了什么。"""

    task_id: str
    action: str                 # shift / curtail / interrupt_resume / forced_violation
    kwh: float                  # 被削减或推迟的电量
    delay_minutes: int = 0      # 移峰导致的最晚完工推迟（0 表示未推迟）
    output_lost_kwh: float = 0.0  # 无法补回、确认损失的产出当量
    extra_starts: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConstraintViolation:
    code: str                   # demand_limit / latest_finish / max_starts / interlock / min_run / ramp / non_interruptible
    task_id: str | None
    slot: str | None
    message: str
    severity: str = "hard"      # hard=未满足；warning=牺牲性提示

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WindowStat:
    window_id: str
    start: str
    end: str
    baseline_demand_kw: float
    planned_demand_kw: float          # 执行版本在窗口关闭时的快照
    actual_demand_kw: float | None
    baseline_energy_cost: float
    planned_energy_cost: float
    actual_energy_cost: float | None
    baseline_total_cost: float
    demand_charge: float              # 计划需量电费
    penalty: float                    # 计划超约罚金
    actual_demand_charge: float | None
    actual_penalty: float | None
    contracted_kw: float
    executed_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskPlanView:
    task_id: str
    kind: str
    status: str                 # locked / holding / must_run / scheduled / shifted / curtailed / protected
    kw: list[float]
    baseline_kw: list[float]
    sacrifice: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProtectionConflict:
    task_id: str
    protection_id: str
    requested_action: str       # 能源侧想做的动作
    resolution: str | None = None  # energy_priority / production_protected / time_limited_exception
    resolved_by: str | None = None
    resolved_at: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanDiff:
    """与上一版计划逐任务比较的结果。"""

    previous_version: int
    changed_tasks: list[dict[str, Any]] = field(default_factory=list)
    baseline_peak_kw: float = 0.0
    previous_peak_kw: float = 0.0
    planned_peak_kw: float = 0.0
    peak_delta_vs_previous_kw: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Plan:
    plan_id: str
    version: int
    state: str                  # calculated / awaiting_arbitration / approved / active / closed
    notice_key: str | None
    merged_retry_seqs: list[int]
    created_at: str
    horizon_start: str
    horizon_end: str
    tasks: list[TaskPlanView]
    total_kw: list[float]
    baseline_total_kw: list[float]
    directive_limit_kw: float | None
    peaks: list[dict[str, Any]]            # 每个需量窗口的峰值与任务归因
    windows: list[WindowStat]
    sacrifices: list[Sacrifice]
    violations: list[ConstraintViolation]
    protection_conflicts: list[ProtectionConflict]
    diff: PlanDiff
    approved_by: str | None = None
    approved_at: str | None = None

    def task_view(self, task_id: str) -> TaskPlanView | None:
        for view in self.tasks:
            if view.task_id == task_id:
                return view
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "state": self.state,
            "notice_key": self.notice_key,
            "merged_retry_seqs": self.merged_retry_seqs,
            "created_at": self.created_at,
            "horizon_start": self.horizon_start,
            "horizon_end": self.horizon_end,
            "tasks": [t.to_dict() for t in self.tasks],
            "total_kw": self.total_kw,
            "baseline_total_kw": self.baseline_total_kw,
            "directive_limit_kw": self.directive_limit_kw,
            "peaks": self.peaks,
            "windows": [w.to_dict() for w in self.windows],
            "sacrifices": [s.to_dict() for s in self.sacrifices],
            "violations": [v.to_dict() for v in self.violations],
            "protection_conflicts": [c.to_dict() for c in self.protection_conflicts],
            "diff": self.diff.to_dict(),
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


def slot_iso(horizon_start: str, index: int) -> str:
    return tg.iso(tg.slot_at(horizon_start, index))
