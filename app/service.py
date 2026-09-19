"""制造负荷与生产承诺协同服务。

职责：
- 版本化滚动计划：初始计划、电网指令、现场强制运行、预测偏差、仲裁均产生新版本；
- 幂等：相同电网通知的重试合并为一次决策；
- 审批流：能源主管批准方案，生产经理提出有期限的保护，冲突进入待仲裁；
- 模拟时钟：推进执行、记录实际用能、进入保温阶段的任务即刻锁定；
- 查询：实际 vs 计划需量、费用差额、未满足约束、保护决定、峰值归因；
- 持久化：JSON 快照，重启后已发生的用能不会被重新调度。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .models import (
    ArbitrationCase,
    ArbitrationResult,
    DemandWindow,
    Device,
    ForecastDeviation,
    ForcedRun,
    GridNotice,
    PlanState,
    PlanVersion,
    Protection,
    ProtectionStatus,
    ScheduledSlot,
    SegmentKind,
    ServiceConfig,
    TaskSpec,
    Tariff,
    UnmetConstraint,
)
from .scheduler import RollingScheduler, ScheduleOutput


class CoordinationService:
    # ------------------------------------------------------------------
    # 构造与持久化
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        config: ServiceConfig,
        devices: dict[str, Device],
        tasks: dict[str, TaskSpec],
        tariff: Tariff,
        windows: dict[str, DemandWindow],
        base_load: Optional[dict[int, float]] = None,
        storage_path: Optional[str] = None,
    ) -> None:
        self.config = config
        self.devices = devices
        self.tasks = tasks
        self.tariff = tariff
        self.windows = windows
        self.base_load_forecast: dict[int, float] = dict(base_load or {})
        self.storage_path = storage_path

        self.now_slot = 0
        self.notices: dict[str, GridNotice] = {}
        self.notice_plans: dict[str, str] = {}          # notice_id -> plan_id（幂等决策）
        self.forced_runs: list[ForcedRun] = []
        self.task_power_scale: dict[str, float] = {}
        self.protections: dict[str, Protection] = {}
        self.cases: dict[str, ArbitrationCase] = {}
        self.plans: list[PlanVersion] = []
        self.executed: dict[str, dict[int, dict[str, Any]]] = {
            tid: {} for tid in tasks
        }
        self.holding_locks: dict[str, dict[int, dict[str, Any]]] = {}
        self.actuals: dict[int, dict[str, float]] = {}  # slot -> source -> kW
        self.incidents: list[UnmetConstraint] = []      # 执行期发现的未满足约束
        self._seq = {"protection": 0, "case": 0}

        self._scheduler = RollingScheduler(
            slot_hours=config.slot_hours,
            slots_per_day=config.slots_per_day,
            devices=devices,
            tasks=tasks,
            tariff=tariff,
            windows=windows,
        )

    @classmethod
    def create(
        cls,
        *,
        config: ServiceConfig,
        devices: list[Device],
        tasks: list[TaskSpec],
        tariff: Tariff,
        windows: list[DemandWindow],
        base_load: Optional[dict[int, float]] = None,
        storage_path: Optional[str] = None,
    ) -> "CoordinationService":
        svc = cls(
            config=config,
            devices={d.id: d for d in devices},
            tasks={t.id: t for t in tasks},
            tariff=tariff,
            windows={w.id: w for w in windows},
            base_load=base_load,
            storage_path=storage_path,
        )
        svc._replan({"type": "initial", "ref": None, "detail": "初始滚动计划"})
        return svc

    def save(self, path: Optional[str] = None) -> None:
        target = path or self.storage_path
        if not target:
            return
        data = {
            "config": self.config.to_dict(),
            "devices": [d.to_dict() for d in self.devices.values()],
            "tasks": [t.to_dict() for t in self.tasks.values()],
            "tariff": self.tariff.to_dict(),
            "windows": [w.to_dict() for w in self.windows.values()],
            "base_load_forecast": {str(s): v for s, v in self.base_load_forecast.items()},
            "now_slot": self.now_slot,
            "notices": [n.to_dict() for n in self.notices.values()],
            "notice_plans": self.notice_plans,
            "forced_runs": [f.to_dict() for f in self.forced_runs],
            "task_power_scale": self.task_power_scale,
            "protections": [p.to_dict() for p in self.protections.values()],
            "cases": [c.to_dict() for c in self.cases.values()],
            "plans": [p.to_dict() for p in self.plans],
            "executed": {
                tid: {str(s): rec for s, rec in slots.items()}
                for tid, slots in self.executed.items()
            },
            "holding_locks": {
                tid: {str(s): rec for s, rec in slots.items()}
                for tid, slots in self.holding_locks.items()
            },
            "actuals": {str(s): dict(src) for s, src in self.actuals.items()},
            "incidents": [i.to_dict() for i in self.incidents],
            "seq": self._seq,
        }
        Path(target).write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str) -> "CoordinationService":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        svc = cls(
            config=ServiceConfig.from_dict(data["config"]),
            devices={d.id: d for d in (Device.from_dict(x) for x in data["devices"])},
            tasks={t.id: t for t in (TaskSpec.from_dict(x) for x in data["tasks"])},
            tariff=Tariff.from_dict(data["tariff"]),
            windows={w.id: w for w in (DemandWindow.from_dict(x) for x in data["windows"])},
            base_load={int(s): v for s, v in data["base_load_forecast"].items()},
            storage_path=path,
        )
        svc.now_slot = data["now_slot"]
        svc.notices = {n.id: n for n in (GridNotice.from_dict(x) for x in data["notices"])}
        svc.notice_plans = dict(data["notice_plans"])
        svc.forced_runs = [ForcedRun.from_dict(x) for x in data["forced_runs"]]
        svc.task_power_scale = dict(data["task_power_scale"])
        svc.protections = {
            p.id: p for p in (Protection.from_dict(x) for x in data["protections"])
        }
        svc.cases = {c.id: c for c in (ArbitrationCase.from_dict(x) for x in data["cases"])}
        svc.plans = [PlanVersion.from_dict(x) for x in data["plans"]]
        svc.executed = {
            tid: {int(s): rec for s, rec in slots.items()}
            for tid, slots in data["executed"].items()
        }
        svc.holding_locks = {
            tid: {int(s): rec for s, rec in slots.items()}
            for tid, slots in data["holding_locks"].items()
        }
        svc.actuals = {int(s): dict(src) for s, src in data["actuals"].items()}
        svc.incidents = [UnmetConstraint.from_dict(x) for x in data["incidents"]]
        svc._seq = dict(data["seq"])
        return svc

    def _persist(self) -> None:
        if self.storage_path:
            self.save(self.storage_path)

    # ------------------------------------------------------------------
    # 计划生成（滚动计算）
    # ------------------------------------------------------------------

    def _build_locked(self) -> dict[str, dict[int, ScheduledSlot]]:
        """汇总不可改写槽位：已执行 + 保温锁 + 生效保护锁。"""
        locked: dict[str, dict[int, ScheduledSlot]] = {}
        for tid, slots in self.executed.items():
            for s, rec in slots.items():
                locked.setdefault(tid, {})[s] = ScheduledSlot(
                    kw=rec["kw"], seg_idx=rec["seg_idx"], source="executed"
                )
        for tid, slots in self.holding_locks.items():
            for s, rec in slots.items():
                if s >= self.now_slot:
                    locked.setdefault(tid, {})[s] = ScheduledSlot(
                        kw=rec["kw"], seg_idx=rec["seg_idx"], source="holding_lock"
                    )
        for p in self.protections.values():
            for s, rec in p.baseline.items():
                if p.locks_slot(s, self.now_slot):
                    locked.setdefault(p.task_id, {})[s] = ScheduledSlot(
                        kw=rec["kw"], seg_idx=rec["seg_idx"], source="protection_lock"
                    )
        return locked

    def _active_notices(self) -> list[GridNotice]:
        return [n for n in self.notices.values() if n.end_slot > self.now_slot]

    def _active_forced(self) -> list[tuple[str, int]]:
        out = []
        for f in self.forced_runs:
            task = self.tasks.get(f.task_id)
            if task and f.start_slot + task.total_slots > self.now_slot:
                out.append((f.task_id, f.start_slot))
        return out

    def _replan(self, trigger: dict[str, Any]) -> PlanVersion:
        # 已过去槽位的基荷采用实际抄表值，未来槽位用最新预测
        base = dict(self.base_load_forecast)
        for t, sources in self.actuals.items():
            if t < self.now_slot and "base" in sources:
                base[t] = sources["base"]
        out: ScheduleOutput = self._scheduler.plan(
            now=self.now_slot,
            horizon=self.config.horizon_slots,
            locked=self._build_locked(),
            forced=self._active_forced(),
            notices=self._active_notices(),
            base_load=base,
            task_power_scale=self.task_power_scale,
        )
        parent = self.plans[-1] if self.plans else None
        # 旧的 calculated 版本被取代
        for p in self.plans:
            if p.state == PlanState.CALCULATED:
                p.state = PlanState.CLOSED
                p.closed_reason = "superseded"
        plan = PlanVersion(
            id=f"v{len(self.plans) + 1}",
            version_no=len(self.plans) + 1,
            parent_id=parent.id if parent else None,
            trigger=trigger,
            created_slot=self.now_slot,
            state=PlanState.CALCULATED,
            assignments=out.assignments,
            warmups=out.warmups,
            site_kw=out.site_kw,
            windows=out.windows,
            total_energy_cost=out.total_energy_cost,
            total_demand_penalty=out.total_demand_penalty,
            total_cost=out.total_cost,
            sacrifices=out.sacrifices,
            unmet=out.unmet,
        )
        plan.diff_summary = self._diff(plan, parent)
        self.plans.append(plan)
        self._persist()
        return plan

    def _diff(self, plan: PlanVersion, parent: Optional[PlanVersion]) -> dict[str, Any]:
        if parent is None:
            return {"trigger": plan.trigger, "moved_tasks": [], "window_deltas": {}}
        moved = []
        for tid in self.tasks:
            a = {
                s: r for s, r in plan.task_slots(tid).items() if r.source != "executed"
            }
            b = {
                s: r for s, r in parent.task_slots(tid).items() if r.source != "executed"
            }
            if {s: r.kw for s, r in a.items()} != {s: r.kw for s, r in b.items()}:
                moved.append(
                    {
                        "task_id": tid,
                        "old_start": min(b) if b else None,
                        "new_start": min(a) if a else None,
                        "old_finish": max(b) + 1 if b else None,
                        "new_finish": max(a) + 1 if a else None,
                    }
                )
        deltas = {}
        for wid, wa in plan.windows.items():
            wb = parent.windows.get(wid)
            if wb is None:
                continue
            deltas[wid] = {
                "peak_before": round(wb.peak_kw, 3),
                "peak_after": round(wa.peak_kw, 3),
                "cost_before": round(wb.total_cost, 3),
                "cost_after": round(wa.total_cost, 3),
            }
        return {
            "trigger": plan.trigger,
            "moved_tasks": moved,
            "window_deltas": deltas,
            "sacrifices": len(plan.sacrifices),
            "unmet": len(plan.unmet),
        }

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------

    def ingest_grid_notice(self, notice: GridNotice) -> PlanVersion:
        """接入电网需量控制通知。相同通知重试合并为一次决策。"""
        existing = self.notices.get(notice.id)
        if existing is not None:
            if existing.payload_key() != notice.payload_key():
                raise ValueError(
                    f"通知 {notice.id} 已存在但载荷不一致，拒绝覆盖（需新通知 id）"
                )
            # 幂等重试：返回既有决策，不产生新版本
            return self.get_plan(self.notice_plans[notice.id])
        notice.received_slot = self.now_slot
        self.notices[notice.id] = notice
        plan = self._replan(
            {
                "type": "grid_notice",
                "ref": notice.id,
                "detail": (
                    f"电网指令 {notice.id}：槽位 {notice.start_slot}-{notice.end_slot} "
                    f"需量上限 {notice.max_demand_kw}kW"
                ),
            }
        )
        self.notice_plans[notice.id] = plan.id
        self._persist()
        return plan

    def apply_forced_run(self, forced: ForcedRun) -> PlanVersion:
        """现场强制运行：任务从指定槽位起连续运行剩余曲线。"""
        if forced.task_id not in self.tasks:
            raise KeyError(f"未知任务 {forced.task_id}")
        self.forced_runs.append(forced)
        return self._replan(
            {
                "type": "forced_run",
                "ref": forced.task_id,
                "detail": f"现场强制运行 {forced.task_id} 自槽位 {forced.start_slot}：{forced.reason}",
            }
        )

    def apply_forecast_deviation(self, deviation: ForecastDeviation) -> PlanVersion:
        """预测偏差：修正基荷预测或任务剩余功率，触发滚动重算。"""
        if deviation.kind == "base_load":
            for t in range(
                max(deviation.start_slot, self.now_slot),
                min(deviation.end_slot, self.config.horizon_slots),
            ):
                self.base_load_forecast[t] = (
                    self.base_load_forecast.get(t, 0.0) + deviation.delta_kw
                )
            detail = (
                f"基荷预测偏差 {deviation.delta_kw:+.1f}kW "
                f"@槽位 {deviation.start_slot}-{deviation.end_slot}"
            )
        elif deviation.kind == "task_power":
            if deviation.task_id not in self.tasks:
                raise KeyError(f"未知任务 {deviation.task_id}")
            cur = self.task_power_scale.get(deviation.task_id, 1.0)
            self.task_power_scale[deviation.task_id] = cur * deviation.scale
            detail = f"任务 {deviation.task_id} 剩余功率预测 x{deviation.scale:.3f}"
        else:
            raise ValueError(f"未知偏差类型 {deviation.kind}")
        return self._replan(
            {"type": "forecast_deviation", "ref": deviation.kind, "detail": detail}
        )

    # ------------------------------------------------------------------
    # 审批、保护与仲裁
    # ------------------------------------------------------------------

    def get_plan(self, plan_id: Optional[str] = None) -> PlanVersion:
        if plan_id is None:
            plan = self._operative_plan()
            if plan is None:
                raise LookupError("尚无计划")
            return plan
        for p in self.plans:
            if p.id == plan_id:
                return p
        raise KeyError(f"未知计划 {plan_id}")

    def _operative_plan(self) -> Optional[PlanVersion]:
        """当前执行所依据的计划：active > 最新 approved > 最新 calculated。"""
        for state in (PlanState.ACTIVE, PlanState.APPROVED, PlanState.CALCULATED):
            for p in reversed(self.plans):
                if p.state == state:
                    return p
        return None

    def _plan_violates(self, plan: PlanVersion, prot: Protection) -> bool:
        """计划在保护期限内是否改写了保护基线。"""
        slots = plan.task_slots(prot.task_id)
        for s, rec in prot.baseline.items():
            if s < prot.created_slot or s >= prot.until_slot:
                continue
            cur = slots.get(s)
            if cur is None or abs(cur.kw - rec["kw"]) > 1e-6:
                return True
        return False

    def approve_plan(self, plan_id: str, by: str = "energy_supervisor") -> PlanVersion:
        """能源主管批准方案。与生效保护冲突时进入待仲裁。"""
        plan = self.get_plan(plan_id)
        if plan.state not in (PlanState.CALCULATED,):
            raise ValueError(f"计划 {plan_id} 当前状态 {plan.state.value} 不可批准")
        conflicts = [
            p
            for p in self.protections.values()
            if p.status in (ProtectionStatus.ACTIVE, ProtectionStatus.UPHELD)
            and self._plan_violates(plan, p)
        ]
        if conflicts:
            plan.state = PlanState.AWAITING_ARBITRATION
            for p in conflicts:
                self._open_case(plan, p, f"批准 {by} 与保护 {p.id}（任务 {p.task_id}）冲突")
            self._persist()
            return plan
        plan.state = PlanState.APPROVED
        self._persist()
        return plan

    def create_protection(
        self,
        task_id: str,
        until_slot: int,
        reason: str,
        by: str = "production_manager",
        expected_start_slot: Optional[int] = None,
    ) -> Protection:
        """生产经理对单个任务提出有期限的保护。

        默认以当前执行计划的安排为基线；给定 expected_start_slot 时，
        以任务剩余曲线从该槽位连续放置为基线。若与已批准/生效计划冲突，
        该计划进入待仲裁。
        """
        if task_id not in self.tasks:
            raise KeyError(f"未知任务 {task_id}")
        task = self.tasks[task_id]
        self._seq["protection"] += 1
        pid = f"p{self._seq['protection']}"
        baseline: dict[int, dict[str, Any]] = {}
        if expected_start_slot is None:
            plan = self._operative_plan()
            if plan is not None:
                for s, rec in plan.task_slots(task_id).items():
                    if s >= self.now_slot and rec.source != "executed":
                        baseline[s] = {"kw": rec.kw, "seg_idx": rec.seg_idx}
        else:
            done = len(self.executed.get(task_id, {}))
            s = max(expected_start_slot, self.now_slot)
            acc = 0
            for idx, seg in enumerate(task.segments):
                for _ in range(seg.duration_slots):
                    if acc >= done:
                        if s < self.config.horizon_slots:
                            baseline[s] = {"kw": seg.power_kw, "seg_idx": idx}
                        s += 1
                    acc += 1
        prot = Protection(
            id=pid,
            task_id=task_id,
            until_slot=until_slot,
            reason=reason,
            created_slot=self.now_slot,
            created_by=by,
            baseline=baseline,
        )
        self.protections[pid] = prot
        # 与已批准/生效计划冲突 → 待仲裁
        for plan in reversed(self.plans):
            if plan.state in (PlanState.APPROVED, PlanState.ACTIVE) and self._plan_violates(
                plan, prot
            ):
                plan.state = PlanState.AWAITING_ARBITRATION
                self._open_case(plan, prot, f"保护 {pid}（任务 {task_id}）与{plan.state.value}计划冲突")
                break
        self._persist()
        return prot

    def _open_case(self, plan: PlanVersion, prot: Protection, reason: str) -> ArbitrationCase:
        self._seq["case"] += 1
        case = ArbitrationCase(
            id=f"c{self._seq['case']}",
            plan_id=plan.id,
            protection_id=prot.id,
            task_id=prot.task_id,
            reason=reason,
            created_slot=self.now_slot,
        )
        self.cases[case.id] = case
        return case

    def arbitrate(
        self, case_id: str, result: ArbitrationResult, by: str = "arbiter"
    ) -> PlanVersion:
        """仲裁冲突：能源优先 / 生产受保护 / 限期例外。"""
        case = self.cases[case_id]
        if case.status != "pending":
            raise ValueError(f"仲裁事项 {case_id} 已结案")
        prot = self.protections[case.protection_id]
        plan = self.get_plan(case.plan_id)
        case.status = "resolved"
        case.result = result.value
        case.resolved_slot = self.now_slot
        case.resolved_by = by
        prot.arbitration_result = result.value

        if result == ArbitrationResult.ENERGY_PRIORITY:
            # 能源优先：保护被覆盖，原计划直接生效
            prot.status = ProtectionStatus.OVERRIDDEN
            plan.state = PlanState.APPROVED
            self._persist()
            return plan
        if result == ArbitrationResult.PRODUCTION_PROTECTED:
            # 生产受保护：整段剩余曲线按基线锁定，重算一版并生效
            prot.status = ProtectionStatus.UPHELD
            prot.scope = "full"
        elif result == ArbitrationResult.TIME_LIMITED_EXCEPTION:
            # 限期例外：仅在保护期限内锁定，期限之外可由能源侧调度
            prot.status = ProtectionStatus.UPHELD
            prot.scope = "window"
        plan.state = PlanState.CLOSED
        plan.closed_reason = f"arbitrated:{result.value}"
        new_plan = self._replan(
            {
                "type": "arbitration",
                "ref": case_id,
                "detail": f"仲裁 {case_id} 结论 {result.value}，重算计划",
            }
        )
        new_plan.state = PlanState.APPROVED
        self._persist()
        return new_plan

    # ------------------------------------------------------------------
    # 模拟时钟
    # ------------------------------------------------------------------

    def _promote_plans(self) -> None:
        """最新 approved 计划生效为 active，原 active 与过期的 approved 关闭。"""
        approved = [p for p in self.plans if p.state == PlanState.APPROVED]
        if not approved:
            return
        newest = approved[-1]
        for p in self.plans:
            if p is newest:
                continue
            if p.state in (PlanState.ACTIVE, PlanState.APPROVED):
                p.state = PlanState.CLOSED
                p.closed_reason = "superseded"
        newest.state = PlanState.ACTIVE

    def advance_clock(
        self,
        to_slot: int,
        actual_overrides: Optional[dict[int, dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """推进模拟时钟。逐槽位结算实际用能：

        actual_overrides: {slot: {"base": kW, "tasks": {task_id: kW}}}，
        缺省按当前执行计划抄表。已执行槽位即刻不可改写；
        进入保温阶段的任务其剩余安排即刻锁定。
        """
        if to_slot <= self.now_slot:
            raise ValueError("时钟只能向前推进")
        to_slot = min(to_slot, self.config.horizon_slots)
        overrides = actual_overrides or {}
        for t in range(self.now_slot, to_slot):
            self._promote_plans()
            plan = self._operative_plan()
            ov = overrides.get(t, {})
            slot_actual = self.actuals.setdefault(t, {})
            slot_actual["base"] = ov.get("base", self.base_load_forecast.get(t, 0.0))
            task_ov = ov.get("tasks", {})
            for tid, task in self.tasks.items():
                planned_kw = 0.0
                if plan is not None:
                    rec = plan.task_slots(tid).get(t)
                    planned_kw = rec.kw if rec else 0.0
                actual_kw = task_ov.get(tid, planned_kw)
                progress = len(self.executed.setdefault(tid, {}))
                if actual_kw > 0:
                    seg_idx, _seg, _off = task.segment_at(min(progress, task.total_slots - 1))
                    self.executed[tid][t] = {"kw": actual_kw, "seg_idx": seg_idx}
                    slot_actual[tid] = actual_kw
                else:
                    # 计划有而实际未跑 → 执行偏差；若处于不可中断段中段 → 事故
                    if planned_kw > 0:
                        self.incidents.append(
                            UnmetConstraint(
                                constraint="planned_not_executed",
                                subject=tid,
                                task_id=tid,
                                slot=t,
                                detail=f"任务 {tid} 槽位 {t} 计划 {planned_kw:.1f}kW 未执行",
                            )
                        )
                    if 0 < progress < task.total_slots:
                        seg_idx, seg, off = task.segment_at(progress)
                        if seg.kind in (SegmentKind.UNINTERRUPTIBLE, SegmentKind.HOLDING) and 0 < off:
                            self.incidents.append(
                                UnmetConstraint(
                                    constraint="interrupted_uninterruptible",
                                    subject=tid,
                                    task_id=tid,
                                    slot=t,
                                    detail=f"任务 {tid} 的{seg.kind.value}段在槽位 {t} 被中断",
                                )
                            )
            # 暖机实际值按计划抄表
            if plan is not None:
                for dev, slots in plan.warmups.items():
                    if t in slots:
                        slot_actual[f"warmup:{dev}"] = slots[t]
            # 进入保温阶段 → 锁定剩余安排
            for tid, task in self.tasks.items():
                if tid in self.holding_locks:
                    continue
                progress = len(self.executed.get(tid, {}))
                if 0 < progress < task.total_slots:
                    seg_idx, seg, off = task.segment_at(progress)
                    if seg.kind == SegmentKind.HOLDING and off > 0 and plan is not None:
                        self.holding_locks[tid] = {
                            s: {"kw": rec.kw, "seg_idx": rec.seg_idx}
                            for s, rec in plan.task_slots(tid).items()
                            if s > t
                        }
        self.now_slot = to_slot
        # 保护到期
        for p in self.protections.values():
            if p.status == ProtectionStatus.ACTIVE and p.until_slot <= self.now_slot:
                p.status = ProtectionStatus.EXPIRED
        self._persist()
        return {
            "now_slot": self.now_slot,
            "now_iso": self.config.slot_to_iso(self.now_slot),
            "operative_plan": self._operative_plan().id if self._operative_plan() else None,
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def compare_plans(
        self, plan_id: str, other_id: Optional[str] = None
    ) -> dict[str, Any]:
        """两版计划对比：峰值/费用变化、被移动的任务、削减代价与未满足约束。"""
        a = self.get_plan(plan_id)
        b = self.get_plan(other_id) if other_id else (
            self.get_plan(a.parent_id) if a.parent_id else None
        )
        result: dict[str, Any] = {
            "plan": a.id,
            "state": a.state.value,
            "trigger": a.trigger,
            "created_slot": a.created_slot,
            "compared_to": b.id if b else None,
            "diff": a.diff_summary if b and b.id == a.parent_id else self._diff(a, b),
            "sacrifices": [s.to_dict() for s in a.sacrifices],
            "unmet": [u.to_dict() for u in a.unmet],
        }
        return result

    def get_peak_attribution(
        self, window_id: str, plan_id: Optional[str] = None
    ) -> dict[str, Any]:
        """峰值来自哪些任务：窗口峰值槽位的功率构成。"""
        plan = self.get_plan(plan_id)
        wa = plan.windows[window_id]
        return {
            "plan_id": plan.id,
            "window_id": window_id,
            "peak_slot": wa.peak_slot,
            "peak_slot_iso": self.config.slot_to_iso(wa.peak_slot),
            "peak_kw": wa.peak_kw,
            "contract_kw": wa.contract_kw,
            "caps": wa.caps,
            "contributions": wa.contributions,
        }

    def _site_actual(self, slot: int) -> float:
        return sum(self.actuals.get(slot, {}).values())

    def get_window_report(self, window_id: str) -> dict[str, Any]:
        """窗口账单视图：实际 vs 计划需量、费用差额、未满足约束。"""
        w = self.windows[window_id]
        plan = self._operative_plan()
        lo, hi = w.start_slot, min(w.end_slot, self.now_slot)
        elapsed = list(range(lo, hi))
        planned_site = plan.site_kw if plan else [0.0] * self.config.horizon_slots
        planned_cost = sum(
            planned_site[t] * self.config.slot_hours * self.tariff.price_at(t)
            for t in elapsed
        )
        actual_cost = sum(
            self._site_actual(t) * self.config.slot_hours * self.tariff.price_at(t)
            for t in elapsed
        )
        planned_peak_elapsed = max((planned_site[t] for t in elapsed), default=None)
        actual_peak = max((self._site_actual(t) for t in elapsed), default=None)
        plan_wa = plan.windows.get(window_id) if plan else None
        unmet = [u.to_dict() for u in (plan.unmet if plan else []) if u.window_id == window_id]
        unmet += [
            i.to_dict()
            for i in self.incidents
            if i.slot is not None and w.start_slot <= i.slot < w.end_slot
        ]
        return {
            "window_id": window_id,
            "start_iso": self.config.slot_to_iso(w.start_slot),
            "end_iso": self.config.slot_to_iso(w.end_slot),
            "contract_kw": w.contract_kw,
            "elapsed_slots": len(elapsed),
            "planned": {
                "plan_id": plan.id if plan else None,
                "peak_kw_full_window": plan_wa.peak_kw if plan_wa else None,
                "peak_kw_elapsed": planned_peak_elapsed,
                "energy_cost_elapsed": round(planned_cost, 4),
                "demand_penalty_full_window": plan_wa.demand_penalty if plan_wa else None,
            },
            "actual": {
                "peak_kw": actual_peak,
                "energy_cost_elapsed": round(actual_cost, 4),
                "demand_penalty_so_far": (
                    round(max(0.0, actual_peak - w.contract_kw) * w.penalty_per_kw, 4)
                    if actual_peak is not None
                    else None
                ),
            },
            "cost_diff_elapsed": round(actual_cost - planned_cost, 4),
            "unmet_constraints": unmet,
            "notices": [
                n.to_dict()
                for n in self.notices.values()
                if n.start_slot < w.end_slot and n.end_slot > w.start_slot
            ],
        }

    def get_task_report(self, task_id: str) -> dict[str, Any]:
        task = self.tasks[task_id]
        plan = self._operative_plan()
        progress = len(self.executed.get(task_id, {}))
        planned = {
            s: rec
            for s, rec in (plan.task_slots(task_id) if plan else {}).items()
            if s >= self.now_slot and rec.source != "executed"
        }
        if progress >= task.total_slots:
            state = "completed"
        elif task_id in self.holding_locks:
            state = "holding_locked"
        elif progress > 0:
            state = "running"
        else:
            state = "not_started"
        starts = sorted(planned)
        blocks = 0
        prev = None
        for s in starts:
            if prev is None or s != prev + 1:
                blocks += 1
            prev = s
        return {
            "task_id": task_id,
            "device_id": task.device_id,
            "state": state,
            "progress_slots": progress,
            "total_slots": task.total_slots,
            "planned_start": min(starts) if starts else None,
            "planned_finish": max(starts) + 1 if starts else None,
            "planned_pauses": max(0, blocks - 1) if starts else 0,
            "protections": [
                {
                    "id": p.id,
                    "until_slot": p.until_slot,
                    "status": p.status.value,
                    "scope": p.scope,
                    "arbitration_result": p.arbitration_result,
                    "reason": p.reason,
                }
                for p in self.protections.values()
                if p.task_id == task_id
            ],
            "unmet_constraints": [
                u.to_dict()
                for u in (plan.unmet if plan else []) + self.incidents
                if u.task_id == task_id
            ],
        }

    def get_protection_decisions(self) -> list[dict[str, Any]]:
        """全部保护决定及其仲裁结论。"""
        out = []
        for p in self.protections.values():
            case = next((c for c in self.cases.values() if c.protection_id == p.id), None)
            out.append(
                {
                    "id": p.id,
                    "task_id": p.task_id,
                    "until_slot": p.until_slot,
                    "status": p.status.value,
                    "scope": p.scope,
                    "arbitration_result": p.arbitration_result,
                    "reason": p.reason,
                    "case": case.to_dict() if case else None,
                }
            )
        return out

    def get_unmet_constraints(self, plan_id: Optional[str] = None) -> list[dict[str, Any]]:
        plan = self.get_plan(plan_id)
        return [u.to_dict() for u in plan.unmet] + [i.to_dict() for i in self.incidents]
