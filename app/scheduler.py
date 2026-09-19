"""滚动计划引擎。

不可变前提：
- now 之前的槽位取自冻结实测（actuals），任何修订不得改写；
- 已进入保温（holding）的任务未来只能按保温功率运行；
- must_run、工艺不可中断区间、已开工的连续任务均锁定。

可决策部分：
- shiftable：整块移峰（最迟完工、互锁、保护约束）；
- interruptible / ramping：窗口内中断，受最少运行、启停次数、恢复爬坡约束。
目标按优先级：满足电网需量限值硬约束 > 少牺牲产出 > 低电价成本。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import timegrid as tg
from .models import Task
from .plan import (
    ConstraintViolation,
    Plan,
    PlanDiff,
    ProtectionConflict,
    Sacrifice,
    TaskPlanView,
    WindowStat,
)

SLOT_H = tg.SLOT_HOURS
EPS = 1e-6


@dataclass
class MovableBlock:
    task: Task
    start: int                    # 当前起点（槽号）
    length: int
    vector: list[float]           # 各槽功率
    locked_positions: dict[int, float]  # 组内互锁参考用：该块之外不可动的占用
    original_start: int
    shifted: bool = False


@dataclass
class InterruptState:
    """interruptible/ramping 任务的规划期状态。"""

    run_slots: list[int] = field(default_factory=list)      # 基线运行槽
    on: dict[int, bool] = field(default_factory=dict)       # 槽 -> 是否运行
    kw: dict[int, float] = field(default_factory=dict)      # 槽 -> 规划功率（含爬坡折减）
    extra_starts: int = 0
    last_start_slot: int = -10_000                          # 最近一次（重）启动槽
    interrupts: list[tuple[int, int]] = field(default_factory=list)  # 中断区间


class Scheduler:
    def __init__(self, warehouse) -> None:
        self.wh = warehouse
        self.tariff = warehouse.tariff
        self.h0 = tg.parse(warehouse.horizon_start)
        self.n = warehouse.horizon_slots
        now = warehouse.clock.now()
        self.now_idx = max(0, tg.slot_index(self.h0, now))
        self.locked = [0.0] * self.n           # 全不可动负荷（含历史/保温/must_run）
        self.base = [0.0] * self.n             # 原计划基线总功率
        self.total = [0.0] * self.n
        self.task_plan: dict[str, list[float]] = {}
        self.task_base: dict[str, list[float]] = {}
        self.task_status: dict[str, str] = {}
        self.blocks: list[MovableBlock] = []
        self.istates: dict[str, InterruptState] = {}
        self.limits: list[float] = [math.inf] * self.n
        self.sacrifices: list[Sacrifice] = []
        self.violations: list[ConstraintViolation] = []
        self.conflicts: list[ProtectionConflict] = []

    # ============================================================== 构建

    def run(self, notice_key: str | None, merged_seqs: list[int], reason: str) -> Plan:
        self._build_limits()
        self._build_tasks()
        self._repair_interlocks()
        self._solve()
        self._verify()
        return self._assemble(notice_key, merged_seqs, reason)

    def _build_limits(self) -> None:
        for d in self.wh.directives.values():
            a = max(0, tg.slot_index(self.h0, d.start))
            b = min(self.n, tg.slot_index(self.h0, d.end))
            for i in range(max(0, a), max(a, b)):
                self.limits[i] = min(self.limits[i], d.demand_limit_kw)

    def _slot_price(self, i: int) -> float:
        return self.tariff.price_at(tg.slot_at(self.h0, i))

    def _is_protected(self, task: Task, i: int) -> bool:
        moment = tg.slot_at(self.h0, i)
        for p in self.wh.active_protections_for(task.task_id, moment):
            if p.resolution in (None, "production_protected", "time_limited_exception"):
                return True
        return False

    def _build_tasks(self) -> None:
        for task in self.wh.tasks.values():
            plan = [0.0] * self.n
            baseline = [0.0] * self.n
            bs = tg.slot_index(self.h0, task.earliest_start)
            be = min(self.n, bs + task.slots)
            started = False
            holding_idx = (
                tg.slot_index(self.h0, task.holding_from)
                if task.holding_from is not None else None
            )
            forced_idxs: set[int] = set()
            for fa, fb in task.forced_segments:
                fia, fib = tg.slot_index(self.h0, fa), tg.slot_index(self.h0, fb)
                for jj in range(max(0, fia), min(self.n, fib)):
                    forced_idxs.add(jj)
            for j in range(max(0, bs), max(0, be)):
                baseline[j] = task.curve_kw_at(j - bs)
            for j in forced_idxs:
                # 强制运行区间（可能超出原计划范围）按额定功率注入基线
                baseline[j] = max(baseline[j], task.power_curve[-1].kw)

            future_locked = [False] * self.n
            holding_end = tg.slot_index(self.h0, task.latest_finish)
            mark_range = range(min([max(0, bs), *forced_idxs] or [max(0, bs)]),
                               max([min(self.n, be), *[i + 1 for i in forced_idxs]]
                                   or [min(self.n, be)]))
            for j in mark_range:
                # 非中断区间
                for a, b in task.non_interruptible:
                    ia, ib = tg.slot_index(self.h0, a), tg.slot_index(self.h0, b)
                    if ia <= j < ib:
                        future_locked[j] = True
                if j in forced_idxs:
                    future_locked[j] = True
                if holding_idx is not None and holding_idx <= j < holding_end:
                    future_locked[j] = True
                if task.kind == "must_run" and j < be:
                    future_locked[j] = True

            for j in range(self.n):
                actual = self.wh.actual_at(tg.iso(tg.slot_at(self.h0, j)))
                if actual is not None:
                    # 已冻结槽位（含时钟刚跨过、已注入实测的槽）不可改写
                    plan[j] = actual.kw_by_task.get(task.task_id, 0.0)
                    if plan[j] > EPS:
                        started = True
                    if j >= self.now_idx:
                        future_locked[j] = True
                    continue
                if j < self.now_idx:
                    plan[j] = baseline[j]
                    if plan[j] > EPS:
                        started = True
                    continue
                if holding_idx is not None and holding_idx <= j < holding_end:
                    plan[j] = task.holding_kw
                    future_locked[j] = True
                elif future_locked[j]:
                    plan[j] = baseline[j]
                elif task.kind == "must_run":
                    plan[j] = baseline[j]
                else:
                    plan[j] = baseline[j]

            # 已开工的连续型任务：剩余运行段锁定（不能中途停）。
            # interruptible/ramping 即使已开工，未来槽仍可中断（受最少运行约束）。
            if started and task.kind in ("shiftable", "must_run"):
                for j in range(max(self.now_idx, bs), be):
                    if baseline[j] > EPS:
                        future_locked[j] = True
                        plan[j] = baseline[j]

            self.task_plan[task.task_id] = plan
            self.task_base[task.task_id] = baseline
            self.task_status[task.task_id] = self._initial_status(task, holding_idx, started)
            self._classify(task, plan, baseline, bs, be, future_locked, started, holding_idx)

        for i in range(self.n):
            self.locked[i] = sum(
                (p[i] if (not self._is_mutable_task(tid) or i < self.now_idx) else 0.0)
                for tid, p in self.task_plan.items()
            )
            self.base[i] = sum(p[i] for p in self.task_base.values())

    def _initial_status(self, task: Task, holding_idx, started: bool) -> str:
        if holding_idx is not None and holding_idx <= self.now_idx:
            return "holding"
        if task.kind == "must_run":
            return "must_run"
        if started:
            return "locked"
        return "scheduled"

    def _is_mutable_task(self, task_id: str) -> bool:
        return task_id in self.istates or any(
            b.task.task_id == task_id for b in self.blocks
        )

    def _classify(self, task, plan, baseline, bs, be, future_locked, started, holding_idx) -> None:
        future_run = [j for j in range(max(self.now_idx, bs), be) if baseline[j] > EPS]
        if not future_run:
            return
        if holding_idx is not None and holding_idx <= self.now_idx:
            return
        if task.kind == "shiftable" and not started and bs >= self.now_idx:
            if any(future_locked[j] for j in future_run):
                return  # 含不可移动片段，整块锁定
            vec = [baseline[j] for j in future_run]
            self.blocks.append(MovableBlock(
                task=task, start=future_run[0], length=len(future_run),
                vector=vec, locked_positions={}, original_start=future_run[0],
            ))
            return
        if task.kind in ("interruptible", "ramping") and not started:
            controllable = [j for j in future_run if not future_locked[j]]
            st = InterruptState(
                run_slots=controllable,
                on={j: True for j in controllable},
                kw={j: baseline[j] for j in controllable},
                last_start_slot=controllable[0] if controllable else self.now_idx,
            )
            self.istates[task.task_id] = st
        elif task.kind in ("interruptible", "ramping") and started:
            # 已开工：全运行范围按实测还原运行/中断轨迹，未来槽默认可中断
            run_range = list(range(max(0, bs), max(0, be)))
            on_map, kw_map = {}, {}
            for j in run_range:
                rec = self.wh.actual_at(tg.iso(tg.slot_at(self.h0, j)))
                if rec is not None:
                    measured = rec.kw_by_task.get(task.task_id, 0.0)
                    on_map[j] = measured > EPS
                    kw_map[j] = measured
                elif j < self.now_idx:
                    on_map[j] = baseline[j] > EPS
                    kw_map[j] = baseline[j]
                else:
                    on_map[j] = True
                    kw_map[j] = baseline[j]
            # 已执行过的中断次数（过去范围内的连续停机段）
            past_off = [j for j in run_range if j < self.now_idx and not on_map.get(j, True)]
            extra = 0
            prev = None
            for j in sorted(past_off):
                if prev is None or j != prev + 1:
                    extra += 1
                prev = j
            # 最近一次启动：从 now 向前找连续运行段起点
            last_start = run_range[0] if run_range else self.now_idx
            j = self.now_idx - 1
            while j in kw_map and on_map.get(j):
                last_start = j
                j -= 1
            st = InterruptState(
                run_slots=future_run, on=on_map, kw=kw_map,
                extra_starts=extra, last_start_slot=last_start,
            )
            self.istates[task.task_id] = st

    # ------------------------------------------------------ 互锁基线修复

    def _repair_interlocks(self) -> None:
        """即使没有需量限值，也必须保证互锁组不同时运行。"""
        for _ in range(1000):
            proj = self._projected()
            victim_slot = None
            victim_task = None
            for i in range(self.now_idx, self.n):
                runners = [
                    self.wh.get_task(tid) for tid in self.istates
                    if self.istates[tid].on.get(i)
                ]
                by_group: dict[str, list] = {}
                for t in runners:
                    if t.interlock_group:
                        by_group.setdefault(t.interlock_group, []).append(t)
                for group, members in by_group.items():
                    if len(members) <= 1:
                        continue
                    # 组内冲突：保留功率大的（通常是主设备），削减小的
                    members.sort(key=lambda t: (t.power_curve[-1].kw, t.task_id))
                    victim_slot = i
                    victim_task = members[0]
                    break
                if victim_task:
                    break
            if victim_task is None:
                return
            cand = self._interrupt_candidate(victim_task, victim_slot, proj,
                                             require_excess=False)
            if cand is None:
                # 最少运行/保护等约束下无法停机：交由 _verify 登记违例
                return
            _, lo, hi, _lost, tail = cand
            self._apply_interrupt(victim_task.task_id, lo, hi, tail)

    # ---------------------------------------------------------- 求解

    def _directive_slots(self) -> list[int]:
        return [i for i in range(self.now_idx, self.n) if math.isfinite(self.limits[i])]

    def _excess(self, i: int) -> float:
        return round(self._projected()[i] - self.limits[i], 6)

    def _projected(self) -> list[float]:
        proj = list(self.locked)
        for b in self.blocks:
            for k, kw in enumerate(b.vector):
                proj[b.start + k] += kw
        for st in self.istates.values():
            for j in st.run_slots:
                if st.on[j]:
                    proj[j] += st.kw[j]
        return proj

    def _solve(self) -> None:
        reported: set[int] = set()
        for _ in range(10_000):
            proj = self._projected()
            target = None
            for i in self._directive_slots():
                if i in reported:
                    continue
                if proj[i] > self.limits[i] + EPS:
                    if target is None or proj[i] - self.limits[i] > proj[target] - self.limits[target]:
                        target = i
            if target is None:
                return
            action = self._best_action(target, proj)
            if action is None:
                self._record_unresolvable(target, proj)
                reported.add(target)
                continue
            kind, payload = action
            if kind == "shift":
                block, new_start = payload
                self._apply_shift(block, new_start)
            else:
                task_id, lo, hi, tail_curtail = payload
                self._apply_interrupt(task_id, lo, hi, tail_curtail)

    # ---------------------------------------------------------- 移峰候选

    def _group_occupied(self, group: str, exclude_task: str) -> list[tuple[int, int]]:
        """互锁组内除 exclude_task 外的占用区间（锁定 + 当前摆放）。"""
        occ = []
        for tid, plan in self.task_plan.items():
            if tid == exclude_task:
                continue
            other = self.wh.get_task(tid)
            if other.interlock_group != group:
                continue
            if tid in self.istates:
                st = self.istates[tid]
                run = [j for j in st.run_slots if st.on[j]]
                if run:
                    occ.append((min(run), max(run) + 1))
            else:
                for j, kw in enumerate(plan):
                    if kw > EPS:
                        occ.append((j, j + 1))
        for b in self.blocks:
            if b.task.task_id == exclude_task or b.task.interlock_group != group:
                continue
            occ.append((b.start, b.start + b.length))
        return occ

    def _group_member_running(self, group: str, exclude_task: str, slot: int) -> bool:
        for lo, hi in self._group_occupied(group, exclude_task):
            if lo <= slot < hi:
                return True
        return False

    def _candidate_starts(self, block: MovableBlock, target: int, proj) -> list[int]:
        task = block.task
        earliest = max(self.now_idx, tg.slot_index(self.h0, task.earliest_start))
        latest_start = tg.slot_index(self.h0, task.latest_finish) - block.length
        if latest_start < earliest:
            return []
        feasible = []
        occupied = self._group_occupied(task.interlock_group, task.task_id) \
            if task.interlock_group else []
        for c in range(earliest, latest_start + 1):
            if c == block.start:
                continue
            if target in range(c, c + block.length):
                continue  # 新摆放必须把功率移出超额槽
            if any(self._is_protected(task, j) for j in range(c, c + block.length)):
                continue
            if any(not (c + block.length <= a or c >= b_) for a, b_ in occupied):
                continue
            # 需量校验：移除旧位、摆放新位后所有槽不超限
            trial = list(proj)
            for k, kw in enumerate(block.vector):
                trial[block.start + k] -= kw
                trial[c + k] += kw
            if all(trial[i] <= self.limits[i] + EPS
                   for i in range(c, c + block.length) if math.isfinite(self.limits[i])):
                feasible.append(c)
        return feasible

    def _shift_score(self, block: MovableBlock, c: int) -> tuple:
        old_cost = sum(self._slot_price(block.start + k) * kw * SLOT_H
                       for k, kw in enumerate(block.vector))
        new_cost = sum(self._slot_price(c + k) * kw * SLOT_H
                       for k, kw in enumerate(block.vector))
        delay = max(0, (c - block.original_start) * tg.SLOT_MINUTES)
        # 排序键：额外电费（削峰场景通常为负收益）、完工延迟、绝对起点
        return (round(new_cost - old_cost, 6), delay, c)

    # ---------------------------------------------------------- 中断候选

    def _interrupt_candidate(self, task: Task, target: int, proj,
                             require_excess: bool = True):
        st = self.istates.get(task.task_id)
        if st is None or not st.on.get(target):
            return None
        if self._is_protected(task, target):
            return None
        min_slots = max(1, task.min_run_minutes // tg.SLOT_MINUTES)
        # 从最近一次启动到 target 之前的连续运行槽数（最少运行约束）
        run_before = 0
        j = target - 1
        while j >= st.last_start_slot and st.on.get(j):
            run_before += 1
            j -= 1
        if run_before < min_slots:
            return None
        # 中断 [target, hi]：覆盖连续超额且该任务在运行的槽
        hi = target
        limit = max(st.run_slots) + 1
        if require_excess:
            while hi + 1 < limit and st.on.get(hi + 1) \
                    and proj[hi + 1] > self.limits[hi + 1] + EPS:
                hi += 1
        tail_curtail = hi + 1 >= limit
        # 恢复点与互锁组内其他设备重叠：把中断延伸到对方运行结束；
        # 若一直延伸到本任务运行段末尾，则尾部削减、不再恢复
        if not tail_curtail and task.interlock_group and \
                self._group_member_running(task.interlock_group, task.task_id, hi + 1):
            while hi + 1 < limit and st.on.get(hi + 1) and \
                    self._group_member_running(task.interlock_group, task.task_id, hi + 1):
                hi += 1
            tail_curtail = hi + 1 >= limit
        # 恢复爬坡后不得超过最迟完工（尾部削减时无恢复）
        ramp_slots = max(1, task.ramp_recover_minutes // tg.SLOT_MINUTES) \
            if task.kind == "ramping" else 0
        if not tail_curtail and hi + 1 + ramp_slots > tg.slot_index(self.h0, task.latest_finish):
            return None
        # 恢复时刻不得与互锁组内其他设备重叠
        if not tail_curtail and task.interlock_group and hi + 1 < self.n and \
                self._group_member_running(task.interlock_group, task.task_id, hi + 1):
            return None
        # 只有需要恢复（重新启动）时才消耗启停次数；任务末尾停机不算重启
        if not tail_curtail and task.max_starts is not None and \
                task.starts_used + st.extra_starts + 1 > task.max_starts:
            return None
        lost_kwh = sum(st.kw[j] * SLOT_H for j in range(target, hi + 1))
        # ramping 恢复段爬坡折减也是一种产出代价
        if not tail_curtail:
            for r in range(1, ramp_slots + 1):
                slot = hi + r
                if slot in st.kw:
                    lost_kwh += st.kw[slot] * SLOT_H * (1 - r / (ramp_slots + 1))
        return (task.task_id, target, hi, lost_kwh, tail_curtail)

    def _best_action(self, target: int, proj):
        best = None
        # 移峰候选：代价最低者优先
        for block in self.blocks:
            if target not in range(block.start, block.start + block.length):
                continue
            if self._is_protected(block.task, target):
                continue
            for c in self._candidate_starts(block, target, proj):
                score = self._shift_score(block, c)
                if best is None or score < best[0]:
                    best = (score, ("shift", (block, c)))
        # 中断候选
        for task in self.wh.tasks.values():
            cand = self._interrupt_candidate(task, target, proj)
            if cand is None:
                continue
            task_id, lo, hi, lost, tail_curtail = cand
            score = (round(lost * 1000, 6), 0, lo)  # 中断牺牲按损失电量排序
            if best is None or score < best[0]:
                best = (score, ("interrupt", (task_id, lo, hi, tail_curtail)))
        return best[1] if best else None

    def _apply_shift(self, block: MovableBlock, new_start: int) -> None:
        task = block.task
        delay = (new_start - block.original_start) * tg.SLOT_MINUTES
        kwh = sum(block.vector) * SLOT_H
        block.start = new_start
        block.shifted = True
        self.task_status[task.task_id] = "shifted"
        self.sacrifices.append(Sacrifice(
            task_id=task.task_id,
            action="shift",
            kwh=round(kwh, 3),
            delay_minutes=max(0, delay),
            detail=f"整块移峰至 {tg.iso(tg.slot_at(self.h0, new_start))}",
        ))

    def _apply_interrupt(self, task_id: str, lo: int, hi: int, tail_curtail: bool = False) -> None:
        task = self.wh.get_task(task_id)
        st = self.istates[task_id]
        lost = sum(st.kw[j] * SLOT_H for j in range(lo, hi + 1))
        for j in range(lo, hi + 1):
            st.on[j] = False
        st.interrupts.append((lo, hi))
        ramp_slots = max(1, task.ramp_recover_minutes // tg.SLOT_MINUTES) \
            if task.kind == "ramping" else 0
        min_slots = max(1, task.min_run_minutes // tg.SLOT_MINUTES)
        self.task_status[task_id] = "curtailed"
        if tail_curtail:
            self.sacrifices.append(Sacrifice(
                task_id=task_id,
                action="curtail",
                kwh=round(lost, 3),
                output_lost_kwh=round(lost, 3),
                detail=(f"{tg.iso(tg.slot_at(self.h0, lo))}~"
                        f"{tg.iso(tg.slot_at(self.h0, hi + 1))} 尾部削减且不恢复"
                        f"（互锁/运行段结束），损失产出 {lost:.1f} kWh"),
            ))
            return
        st.extra_starts += 1
        resume = hi + 1
        st.last_start_slot = resume
        for r in range(1, ramp_slots + 1):
            slot = resume + r - 1
            if slot in st.kw:
                factor = r / (ramp_slots + 1)
                lost += st.kw[slot] * SLOT_H * (1 - factor)
                st.kw[slot] *= factor
        self.sacrifices.append(Sacrifice(
            task_id=task_id,
            action="interrupt_resume",
            kwh=round(lost, 3),
            output_lost_kwh=round(lost, 3),
            extra_starts=1,
            detail=(f"{tg.iso(tg.slot_at(self.h0, lo))}~"
                    f"{tg.iso(tg.slot_at(self.h0, hi + 1))} 中断，"
                    f"{tg.iso(tg.slot_at(self.h0, resume))} 恢复"
                    + (f"，爬坡 {task.ramp_recover_minutes} 分钟" if ramp_slots else "")
                    + f"（最少运行 {min_slots*tg.SLOT_MINUTES} 分钟/次）"),
        ))

    def _record_unresolvable(self, target: int, proj) -> None:
        moment = tg.iso(tg.slot_at(self.h0, target))
        contributors = []
        for tid, plan in self.task_plan.items():
            task = self.wh.get_task(tid)
            share = self._task_share_at(tid, target)
            if share > EPS:
                contributors.append((tid, task.name, share, self._is_protected(task, target)))
        contributors.sort(key=lambda x: -x[2])
        protected = [c for c in contributors if c[3]]
        for tid, name, share, _ in protected:
            protection = None
            for p in self.wh.active_protections_for(tid, tg.slot_at(self.h0, target)):
                protection = p
                break
            if any(c.task_id == tid for c in self.conflicts):
                continue
            self.conflicts.append(ProtectionConflict(
                task_id=tid,
                protection_id=protection.protection_id if protection else "",
                requested_action="move_or_interrupt",
                resolution=protection.resolution if protection else None,
                note=f"槽位 {moment} 需量缺口 {proj[target]-self.limits[target]:.1f} kW，"
                     f"受保护任务 {name} 占用 {share:.1f} kW",
            ))
        self.violations.append(ConstraintViolation(
            code="demand_limit",
            task_id=None,
            slot=moment,
            message=(f"槽位 {moment} 需量 {proj[target]:.1f} kW 超过限值 "
                     f"{self.limits[target]:.1f} kW，峰值来自："
                     + "；".join([f"{n} {s:.1f}kW" for _, n, s, _ in contributors[:4]])
                     + ("；存在受保护任务，待仲裁" if protected else "；无可行移峰/中断手段")),
        ))

    def _task_share_at(self, task_id: str, i: int) -> float:
        for b in self.blocks:
            if b.task.task_id == task_id and b.start <= i < b.start + b.length:
                return b.vector[i - b.start]
        st = self.istates.get(task_id)
        if st and st.on.get(i):
            return st.kw[i]
        plan = self.task_plan.get(task_id)
        return plan[i] if plan else 0.0

    # ============================================================== 校验

    def _verify(self) -> None:
        for b in self.blocks:
            task = b.task
            if b.start + b.length > tg.slot_index(self.h0, task.latest_finish):
                self.violations.append(ConstraintViolation(
                    code="latest_finish", task_id=task.task_id, slot=None,
                    message=f"{task.name} 摆放到 {tg.iso(tg.slot_at(self.h0, b.start+b.length))} "
                            f"晚于最迟完工 {task.latest_finish}"))
        for task_id, st in self.istates.items():
            task = self.wh.get_task(task_id)
            if task.max_starts is not None and \
                    task.starts_used + st.extra_starts > task.max_starts:
                self.violations.append(ConstraintViolation(
                    code="max_starts", task_id=task_id, slot=None,
                    message=f"{task.name} 启停次数 {task.starts_used + st.extra_starts} "
                            f"超过上限 {task.max_starts}"))
        # 互锁校验
        groups: dict[str, list[tuple[int, int, str]]] = {}
        for tid, plan in self._final_plans().items():
            task = self.wh.get_task(tid)
            if not task.interlock_group:
                continue
            running = [i for i, kw in enumerate(plan) if kw > EPS]
            if running:
                groups.setdefault(task.interlock_group, []).append(
                    (min(running), max(running) + 1, tid))
        for group, ints in groups.items():
            ints.sort()
            for (a, b, t1), (c, d, _t2) in zip(ints, ints[1:]):
                if c < b:
                    self.violations.append(ConstraintViolation(
                        code="interlock", task_id=t1,
                        slot=tg.iso(tg.slot_at(self.h0, c)),
                        message=f"互锁组 {group} 在 {tg.iso(tg.slot_at(self.h0,c))} 同时运行"))

    def _final_plans(self) -> dict[str, list[float]]:
        plans = {tid: list(p) for tid, p in self.task_plan.items()}
        for b in self.blocks:
            tid = b.task.task_id
            plans[tid] = [0.0] * self.n
            for k, kw in enumerate(b.vector):
                plans[tid][b.start + k] = kw
        for tid, st in self.istates.items():
            # 以初始计划（含历史实测、不可中断/保温锁定槽）为底，仅覆盖可控槽
            plans[tid] = list(self.task_plan[tid])
            for j in st.run_slots:
                plans[tid][j] = st.kw[j] if st.on[j] else 0.0
        # 历史槽回填（双保险）
        for tid in plans:
            for j in range(self.now_idx):
                plans[tid][j] = self.task_plan[tid][j]
        return plans

    # ============================================================== 组装

    def _assemble(self, notice_key, merged_seqs, reason) -> Plan:
        plans = self._final_plans()
        total = [sum(p[i] for p in plans.values()) for i in range(self.n)]
        peaks = self._peak_attribution(plans, total)
        windows = self._window_stats(plans, total)
        limit = None
        if notice_key:
            d = self.wh.directives.get(notice_key)
            limit = d.demand_limit_kw if d else None
        prev = self.wh.latest_plan()
        diff = self._diff(prev, plans, total, reason)
        state = "awaiting_arbitration" if self.conflicts else "calculated"
        views = []
        for tid, plan in plans.items():
            task = self.wh.get_task(tid)
            sac = next((s for s in self.sacrifices if s.task_id == tid), None)
            views.append(TaskPlanView(
                task_id=tid, kind=task.kind,
                status=self.task_status[tid],
                kw=[round(x, 3) for x in plan],
                baseline_kw=[round(x, 3) for x in self.task_base[tid]],
                sacrifice=sac.to_dict() if sac else None,
            ))
        directive = self.wh.directives.get(notice_key) if notice_key else None
        return Plan(
            plan_id=f"plan_{len(self.wh.plans) + 1:03d}",
            version=len(self.wh.plans) + 1,
            state=state,
            notice_key=notice_key,
            merged_retry_seqs=sorted(merged_seqs),
            created_at=self.wh.clock.now_iso(),
            horizon_start=tg.iso(self.h0),
            horizon_end=tg.iso(tg.slot_at(self.h0, self.n)),
            tasks=views,
            total_kw=[round(x, 3) for x in total],
            baseline_total_kw=[round(x, 3) for x in self.base],
            directive_limit_kw=limit,
            peaks=peaks,
            windows=windows,
            sacrifices=self.sacrifices,
            violations=self.violations,
            protection_conflicts=self.conflicts,
            diff=diff,
        )

    def _peak_attribution(self, plans, total) -> list[dict]:
        peaks = []
        ranges = []
        for d in self.wh.directives.values():
            a = max(self.now_idx, tg.slot_index(self.h0, d.start))
            b = min(self.n, tg.slot_index(self.h0, d.end))
            if a < b:
                ranges.append((f"directive:{d.notice_key}", a, b))
        for w in self.wh.windows:
            a = max(0, tg.slot_index(self.h0, w.start))
            b = min(self.n, tg.slot_index(self.h0, w.end))
            if a < b:
                ranges.append((w.window_id, a, b))
        seen = set()
        for rid, a, b in ranges:
            key = (rid, a, b)
            if key in seen:
                continue
            seen.add(key)
            i = max(range(a, b), key=lambda x: total[x])
            attr = sorted(
                ((tid, plans[tid][i], self.wh.get_task(tid).name)
                 for tid in plans if plans[tid][i] > EPS),
                key=lambda x: -x[1],
            )
            peaks.append({
                "range": rid,
                "slot": tg.iso(tg.slot_at(self.h0, i)),
                "peak_kw": round(total[i], 3),
                "limit_kw": self.limits[i] if math.isfinite(self.limits[i]) else None,
                "attribution": [
                    {"task_id": tid, "name": name, "kw": round(kw, 3)}
                    for tid, kw, name in attr
                ],
            })
        return peaks

    def _energy_cost(self, kw_array, a, b) -> float:
        return sum(kw_array[i] * self._slot_price(i) * SLOT_H for i in range(a, b))

    def _window_stats(self, plans, total) -> list[WindowStat]:
        stats = []

        def charge(demand):
            dc = self.tariff.demand_charge * demand
            pen = max(0.0, demand - self.tariff.contracted_demand_kw) \
                * self.tariff.demand_penalty_per_kw
            return round(dc, 2), round(pen, 2)

        for w in self.wh.windows:
            a = max(0, tg.slot_index(self.h0, w.start))
            b = min(self.n, tg.slot_index(self.h0, w.end))
            if b <= 0 or a >= self.n:
                continue
            bb = min(b, self.n)
            base_demand = max(self.base[a:bb]) if bb > a else 0.0
            dc_b, pen_b = charge(base_demand)
            base_energy = self._energy_cost(self.base, a, bb)
            snapshot = self.wh.window_snapshot(w.window_id)
            closed = b <= self.now_idx

            actual_demand = actual_cost = None
            actual_dc = actual_pen = None
            if closed:
                act_arr = []
                for i in range(a, bb):
                    rec = self.wh.actual_at(tg.iso(tg.slot_at(self.h0, i)))
                    act_arr.append(rec.total if rec else 0.0)
                actual_demand = max(act_arr) if act_arr else 0.0
                actual_cost = sum(
                    (self.wh.actual_at(tg.iso(tg.slot_at(self.h0, i))).total
                     if self.wh.actual_at(tg.iso(tg.slot_at(self.h0, i))) else 0.0)
                    * self._slot_price(i) * SLOT_H for i in range(a, bb))
                actual_dc, actual_pen = charge(actual_demand)

            if snapshot is not None:
                # 已关闭窗口：计划值取关闭瞬间的执行版本快照，不再被后续重算改写
                plan_demand = snapshot["planned_demand_kw"]
                plan_energy = snapshot["planned_energy_cost"]
                dc_p = snapshot["demand_charge"]
                pen_p = snapshot["penalty"]
                executed_version = snapshot["version"]
            else:
                plan_demand = max(total[a:bb]) if bb > a else 0.0
                plan_energy = self._energy_cost(total, a, bb)
                dc_p, pen_p = charge(plan_demand)
                executed_version = None

            stats.append(WindowStat(
                window_id=w.window_id, start=w.start, end=w.end,
                baseline_demand_kw=round(base_demand, 3),
                planned_demand_kw=round(plan_demand, 3),
                actual_demand_kw=round(actual_demand, 3) if actual_demand is not None else None,
                baseline_energy_cost=round(base_energy, 2),
                planned_energy_cost=round(plan_energy, 2),
                actual_energy_cost=round(actual_cost, 2) if actual_cost is not None else None,
                baseline_total_cost=round(base_energy + dc_b + pen_b, 2),
                demand_charge=dc_p,
                penalty=pen_p,
                actual_demand_charge=actual_dc,
                actual_penalty=actual_pen,
                contracted_kw=self.tariff.contracted_demand_kw,
                executed_version=executed_version,
            ))
        return stats

    def _diff(self, prev, plans, total, reason) -> PlanDiff:
        changed = []
        if prev:
            prev_views = {v["task_id"]: v for v in prev["tasks"]}
            for tid, plan in plans.items():
                old = prev_views.get(tid)
                if old is None:
                    changed.append({"task_id": tid, "change": "new"})
                    continue
                delta = max((abs(plan[i] - old["kw"][i]) for i in range(self.n)), default=0)
                if delta > EPS or self.task_status[tid] != old["status"]:
                    moved = sum(1 for i in range(self.n)
                                if abs(plan[i] - old["kw"][i]) > EPS)
                    changed.append({
                        "task_id": tid,
                        "change": "revised",
                        "status_from": old["status"],
                        "status_to": self.task_status[tid],
                        "max_slot_delta_kw": round(delta, 3),
                        "slots_changed": moved,
                    })
            prev_peak = max(prev["total_kw"][self.now_idx:], default=0.0)
        else:
            prev_peak = 0.0
        future_peak = max(total[self.now_idx:], default=0.0)
        base_peak = max(self.base[self.now_idx:], default=0.0)
        return PlanDiff(
            previous_version=prev["version"] if prev else 0,
            changed_tasks=changed,
            baseline_peak_kw=round(base_peak, 3),
            previous_peak_kw=round(prev_peak, 3),
            planned_peak_kw=round(future_peak, 3),
            peak_delta_vs_previous_kw=round(future_peak - prev_peak, 3) if prev else 0.0,
            note=reason,
        )
