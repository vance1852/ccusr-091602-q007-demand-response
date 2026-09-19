"""仓库：持久化全部输入与不可变历史；模拟时钟；决定记录。

重启服务后从同一 JSON 仓库恢复，已经发生的用能片段（actuals）冻结，
任何重算都只能改变 now 之后的槽位。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import timegrid as tg
from .models import BillingWindow, GridDirective, TariffSchedule, Task


@dataclass
class Protection:
    """生产经理对单个任务提出的有期限保护。"""

    protection_id: str
    task_id: str
    start: str
    end: str
    reason: str
    created_by: str = "production_manager"
    created_at: str = ""
    # resolution 由仲裁写入：energy_priority / production_protected / time_limited_exception
    resolution: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None

    def __post_init__(self) -> None:
        self.start = tg.iso(tg.floor_slot(self.start))
        self.end = tg.iso(tg.floor_slot(self.end))

    def active_at(self, moment) -> bool:
        moment = tg.iso(tg.floor_slot(moment))
        return self.start <= moment < self.end

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActualSlot:
    """已冻结的实测槽位（不可改写）。"""

    slot: str
    kw_by_task: dict[str, float]

    @property
    def total(self) -> float:
        return round(sum(self.kw_by_task.values()), 6)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SimulationClock:
    """可显式推进的模拟时钟。"""

    def __init__(self, start) -> None:
        self._now = tg.floor_slot(start)

    def now(self):
        return self._now

    def now_iso(self) -> str:
        return tg.iso(self._now)

    def advance(self, minutes: int) -> None:
        if minutes <= 0 or minutes % tg.SLOT_MINUTES != 0:
            raise ValueError("推进时长必须为正且对齐槽位")
        self._now = self._now + timedelta(minutes=minutes)

    def set(self, moment) -> None:
        self._now = tg.floor_slot(moment)


class Warehouse:
    """线程安全的 JSON 持久化仓库。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self.path = Path(path) if path else None
        self.clock: SimulationClock | None = None
        self.tariff: TariffSchedule | None = None
        self.windows: list[BillingWindow] = []
        self.tasks: dict[str, Task] = {}
        self.directives: dict[str, GridDirective] = {}      # notice_key -> 最新一份
        self.directive_retry_seqs: dict[str, list[int]] = {}
        self.protections: list[Protection] = []
        self.actuals: dict[str, ActualSlot] = {}            # slot iso -> 实测
        self.window_snapshots: dict[str, dict[str, Any]] = {}  # 窗口关闭时的计划快照
        self.plans: list[dict[str, Any]] = []
        self.horizon_start: str | None = None
        self.horizon_slots: int = 96 * 2
        if self.path and self.path.exists():
            self._load()

    # ------------------------------------------------------------ 配置

    def configure(self, tariff: TariffSchedule, windows: list[BillingWindow],
                  horizon_start, horizon_slots: int = 192, clock_start=None) -> None:
        with self._lock:
            self.tariff = tariff
            self.windows = sorted(windows, key=lambda w: w.start)
            self.horizon_start = tg.iso(tg.floor_slot(horizon_start))
            self.horizon_slots = horizon_slots
            self.clock = SimulationClock(clock_start or horizon_start)
            self._save()

    def upsert_task(self, task: Task) -> None:
        with self._lock:
            self.tasks[task.task_id] = task
            self._save()

    def get_task(self, task_id: str) -> Task:
        with self._lock:
            return self.tasks[task_id]

    # ------------------------------------------------------ 电网通知（幂等合并）

    def receive_directive(self, directive: GridDirective) -> tuple[GridDirective, bool]:
        """返回 (有效通知, 是否触发重算)。相同 notice_key+参数的重试被合并。"""
        with self._lock:
            key = directive.notice_key
            seen = self.directive_retry_seqs.setdefault(key, [])
            previous = self.directives.get(key)
            if previous is not None and (
                previous.start == directive.start
                and previous.end == directive.end
                and abs(previous.demand_limit_kw - directive.demand_limit_kw) < 1e-9
            ):
                # 相同通知的重试：只记录 seq，不产生新决策
                if directive.retry_seq not in seen:
                    seen.append(directive.retry_seq)
                    self._save()
                return previous, False
            if directive.retry_seq not in seen:
                seen.append(directive.retry_seq)
            self.directives[key] = directive
            self._save()
            return directive, True

    def active_directives(self, moment=None) -> list[GridDirective]:
        with self._lock:
            moment = tg.iso(moment or self.clock.now())
            return [d for d in self.directives.values() if d.start <= moment < d.end
                    or (moment < d.start)]

    def current_directive_for(self, start_iso: str, end_iso: str):
        """与给定区间相交的、需量限制最严的通知。"""
        with self._lock:
            candidates = [
                d for d in self.directives.values()
                if d.start < end_iso and d.end > start_iso
            ]
            return min(candidates, key=lambda d: d.demand_limit_kw, default=None)

    # ------------------------------------------------------------ 保护

    def add_protection(self, protection: Protection) -> Protection:
        with self._lock:
            protection.created_at = protection.created_at or self.clock.now_iso()
            self.protections.append(protection)
            self._save()
            return protection

    def resolve_protection(self, protection_id: str, resolution: str, by: str) -> Protection:
        with self._lock:
            for p in self.protections:
                if p.protection_id == protection_id:
                    p.resolution = resolution
                    p.resolved_by = by
                    p.resolved_at = self.clock.now_iso()
                    self._save()
                    return p
            raise KeyError(protection_id)

    def active_protections_for(self, task_id: str, moment) -> list[Protection]:
        moment = tg.iso(tg.floor_slot(moment))
        with self._lock:
            return [
                p for p in self.protections
                if p.task_id == task_id and p.start <= moment < p.end
                and p.resolution != "energy_priority"
            ]

    # ------------------------------------------------------------ 实测冻结

    def record_actuals(self, kw_by_task_by_slot: dict[str, dict[str, float]]) -> None:
        """冻结历史槽位。已存在的槽位拒绝改写（已执行功率片段不可改写）。"""
        with self._lock:
            for slot, kw_by_task in kw_by_task_by_slot.items():
                slot_iso = tg.iso(tg.floor_slot(slot))
                if slot_iso in self.actuals:
                    existing = self.actuals[slot_iso].kw_by_task
                    if {k: round(v, 6) for k, v in existing.items()} != \
                            {k: round(v, 6) for k, v in kw_by_task.items()}:
                        raise PermissionError(f"槽位 {slot_iso} 的实测已冻结，不可改写")
                    continue
                self.actuals[slot_iso] = ActualSlot(slot_iso, dict(kw_by_task))
            self._save()

    def is_frozen(self, slot_iso: str) -> bool:
        return tg.iso(tg.floor_slot(slot_iso)) in self.actuals

    def actual_at(self, slot_iso: str) -> ActualSlot | None:
        return self.actuals.get(tg.iso(tg.floor_slot(slot_iso)))

    def snapshot_window(self, window_id: str, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self.window_snapshots.setdefault(window_id, snapshot)
            self._save()

    def window_snapshot(self, window_id: str) -> dict[str, Any] | None:
        with self._lock:
            snap = self.window_snapshots.get(window_id)
            return dict(snap) if snap else None

    # ------------------------------------------------------------ 计划版本

    def append_plan(self, plan_dict: dict[str, Any]) -> None:
        with self._lock:
            self.plans.append(plan_dict)
            self._save()

    def latest_plan(self) -> dict[str, Any] | None:
        with self._lock:
            return self.plans[-1] if self.plans else None

    def plan_by_version(self, version: int) -> dict[str, Any] | None:
        with self._lock:
            for plan in self.plans:
                if plan["version"] == version:
                    return plan
            return None

    def update_plan_state(self, version: int, **changes) -> dict[str, Any]:
        with self._lock:
            for plan in self.plans:
                if plan["version"] == version:
                    plan.update(changes)
                    self._save()
                    return plan
            raise KeyError(f"version {version}")

    # ------------------------------------------------------------ 时钟

    def advance_clock(self, minutes: int) -> None:
        with self._lock:
            self.clock.advance(minutes)
            self._save()

    def horizon(self) -> tuple[str, int]:
        return self.horizon_start, self.horizon_slots

    # ------------------------------------------------------------ 持久化

    def _save(self) -> None:
        if not self.path:
            return
        payload = {
            "clock": self.clock.now_iso() if self.clock else None,
            "horizon_start": self.horizon_start,
            "horizon_slots": self.horizon_slots,
            "tariff": self.tariff.to_dict() if self.tariff else None,
            "windows": [asdict(w) for w in self.windows],
            "tasks": {tid: t.to_dict() for tid, t in self.tasks.items()},
            "directives": {k: d.to_dict() for k, d in self.directives.items()},
            "directive_retry_seqs": self.directive_retry_seqs,
            "protections": [p.to_dict() for p in self.protections],
            "actuals": [a.to_dict() for a in sorted(self.actuals.values(), key=lambda x: x.slot)],
            "window_snapshots": self.window_snapshots,
            "plans": self.plans,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.horizon_start = data["horizon_start"]
        self.horizon_slots = data["horizon_slots"]
        self.clock = SimulationClock(data["clock"] or data["horizon_start"])
        self.tariff = TariffSchedule.from_dict(data["tariff"]) if data.get("tariff") else None
        self.windows = [BillingWindow(**w) for w in data.get("windows", [])]
        self.tasks = {tid: Task.from_dict(td) for tid, td in data.get("tasks", {}).items()}
        self.directives = {
            k: GridDirective.from_dict(dd) for k, dd in data.get("directives", {}).items()
        }
        self.directive_retry_seqs = {
            k: list(v) for k, v in data.get("directive_retry_seqs", {}).items()
        }
        self.protections = [Protection(**p) for p in data.get("protections", [])]
        self.actuals = {}
        for a in data.get("actuals", []):
            self.actuals[a["slot"]] = ActualSlot(a["slot"], a["kw_by_task"])
        self.window_snapshots = dict(data.get("window_snapshots", {}))
        self.plans = data.get("plans", [])
