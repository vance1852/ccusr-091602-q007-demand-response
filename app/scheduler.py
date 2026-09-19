"""滚动调度引擎。

输入：设备、任务（含工艺不可中断区间/保温段）、分时电价、需量窗口、
电网指令上限、已锁定槽位（已执行/保温锁/保护锁）、强制运行与基荷预测。
输出：一版完整计划（含已锁定槽位的原样回显）、暖机爬坡安排、
未满足约束、削减代价（sacrifices）与逐窗口分析。

算法：按优先级逐任务用动态规划选择各曲线段的放置槽位
（不可中断/保温段整体连续；interruptible 任务的普通段可拆分到槽位粒度），
硬约束（设备互锁、设备容量、恢复爬坡、每日启停次数）在转移时检查，
电网指令上限以高额罚分软约束；全部放置后再做削峰修复：
把贡献峰值的可移峰任务整体重排，并记录每次移动牺牲了什么。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import (
    EPS,
    DemandWindow,
    Device,
    LoadKind,
    Sacrifice,
    ScheduledSlot,
    SegmentKind,
    TaskSpec,
    Tariff,
    UnmetConstraint,
    WindowAnalysis,
)

# 罚分量级：电网指令越限 >> 启停溢出 > 最迟完工 > 修复推动 > 电费
NOTICE_EXCESS_PENALTY = 1e6    # 每 kW·槽位 越过电网指令上限
PAUSE_OVERFLOW_PENALTY = 2e5   # 每多一次超过 max_pauses 的中断
LATE_PENALTY = 5e4             # 每超过最迟完工一个槽位
FORBID_SLOT_PENALTY = 3e5      # 修复阶段推动任务离开越限槽位
START_BIAS = 1e3               # must_run 尽早启动的偏置
MAX_REPAIR_ITERS = 12


@dataclass
class _Unit:
    """任务剩余曲线展开后的最小放置单元。atomic 段为长度>1 的整块。"""

    seg_idx: int
    length: int
    kw: float


@dataclass
class _Placement:
    starts: list[int] = field(default_factory=list)
    cost: float = 0.0
    pauses: int = 0
    late_slots: int = 0
    pause_overflow: int = 0
    infeasible: bool = False
    warmups: list[int] = field(default_factory=list)  # 提交时确定的暖机槽位


class _Board:
    """调度占用面板：记录已放置（含锁定）的任务功率与暖机功率。"""

    def __init__(self, horizon: int) -> None:
        self.horizon = horizon
        self.device_kw: list[dict[str, float]] = [dict() for _ in range(horizon)]
        self.task_kw: list[dict[str, float]] = [dict() for _ in range(horizon)]
        self.warmup_kw: list[dict[str, float]] = [dict() for _ in range(horizon)]

    def device_on(self, device_id: str, slot: int) -> bool:
        if slot < 0 or slot >= self.horizon:
            return False
        return (
            self.device_kw[slot].get(device_id, 0.0) > EPS
            or self.warmup_kw[slot].get(device_id, 0.0) > EPS
        )


@dataclass
class _Context:
    now: int
    horizon: int
    slot_hours: float
    slots_per_day: int
    board: _Board
    base_load: dict[int, float]
    prices: Tariff
    caps: dict[int, float]          # slot -> 电网指令上限（无指令则无项）
    locked: dict[str, dict[int, ScheduledSlot]]
    task_power_scale: dict[str, float]

    def cap_at(self, slot: int) -> float:
        return self.caps.get(slot, float("inf"))

    def site_kw_at(self, slot: int) -> float:
        b = self.board
        return (
            self.base_load.get(slot, 0.0)
            + sum(b.device_kw[slot].values())
            + sum(b.warmup_kw[slot].values())
        )

    def locked_has(self, task_id: str, slot: int) -> bool:
        return slot in self.locked.get(task_id, {})


@dataclass
class ScheduleOutput:
    assignments: dict[str, dict[int, ScheduledSlot]]
    warmups: dict[str, dict[int, float]]
    site_kw: list[float]
    windows: dict[str, WindowAnalysis]
    total_energy_cost: float
    total_demand_penalty: float
    total_cost: float
    sacrifices: list[Sacrifice]
    unmet: list[UnmetConstraint]


class RollingScheduler:
    def __init__(
        self,
        *,
        slot_hours: float,
        slots_per_day: int,
        devices: dict[str, Device],
        tasks: dict[str, TaskSpec],
        tariff: Tariff,
        windows: dict[str, DemandWindow],
    ) -> None:
        self.slot_hours = slot_hours
        self.slots_per_day = slots_per_day
        self.devices = devices
        self.tasks = tasks
        self.tariff = tariff
        self.windows = windows
        # 互锁组索引
        self._interlocked: dict[str, set[str]] = {d: set() for d in devices}
        for d in devices.values():
            if d.interlock_group:
                for other in devices.values():
                    if other.id != d.id and other.interlock_group == d.interlock_group:
                        self._interlocked[d.id].add(other.id)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def plan(
        self,
        *,
        now: int,
        horizon: int,
        locked: dict[str, dict[int, ScheduledSlot]],
        forced: list[tuple[str, int]],          # (task_id, start_slot)
        notices: list[Any],                     # GridNotice
        base_load: dict[int, float],
        task_power_scale: Optional[dict[str, float]] = None,
    ) -> ScheduleOutput:
        board = _Board(horizon)
        caps: dict[int, float] = {}
        for n in notices:
            for t in range(max(n.start_slot, now), min(n.end_slot, horizon)):
                caps[t] = min(caps.get(t, float("inf")), n.max_demand_kw)
        ctx = _Context(
            now=now,
            horizon=horizon,
            slot_hours=self.slot_hours,
            slots_per_day=self.slots_per_day,
            board=board,
            base_load=base_load,
            prices=self.tariff,
            caps=caps,
            locked=locked,
            task_power_scale=task_power_scale or {},
        )
        unmet: list[UnmetConstraint] = []
        sacrifices: list[Sacrifice] = []

        # 1) 锁定槽位上板（已执行/保温锁/保护锁），不可改写
        for tid, slots in locked.items():
            task = self.tasks.get(tid)
            if task is None:
                continue
            dev = self.devices[task.device_id]
            for s, rec in slots.items():
                if 0 <= s < horizon:
                    board.task_kw[s][tid] = board.task_kw[s].get(tid, 0.0) + rec.kw
                    board.device_kw[s][dev.id] = board.device_kw[s].get(dev.id, 0.0) + rec.kw

        # 2) 逐任务放置：强制运行 > must_run > 其余（按最迟完工、优先级）
        forced_map = {tid: start for tid, start in forced}
        placements: dict[str, tuple[list[_Unit], _Placement, str]] = {}

        def place(tid: str, source: str, **kwargs: Any) -> None:
            units = self._remaining_units(ctx, tid)
            if not units:
                return
            task = self.tasks[tid]
            device = self.devices[task.device_id]
            placement = self._dp_place(ctx, task, device, units, **kwargs)
            placements[tid] = (units, placement, source)
            self._commit(ctx, task, device, units, placement)
            if placement.infeasible:
                unmet.append(
                    UnmetConstraint(
                        constraint="placement_infeasible",
                        subject=tid,
                        task_id=tid,
                        detail=f"任务 {tid} 在约束下无可行排程，已按最早位置强制放下",
                    )
                )
            if placement.pause_overflow > 0:
                unmet.append(
                    UnmetConstraint(
                        constraint="max_pauses",
                        subject=tid,
                        task_id=tid,
                        detail=f"任务 {tid} 需要 {placement.pauses} 次中断，超出允许值 {task.max_pauses}",
                    )
                )

        for tid, start in sorted(forced_map.items(), key=lambda kv: kv[1]):
            if tid in self.tasks:
                place(
                    tid,
                    "forced",
                    fixed_start=max(start, now),
                    ignore_caps=True,
                    max_pauses_override=0,
                    hard_pauses=True,
                )
        for tid, task in sorted(
            self.tasks.items(), key=lambda kv: (kv[1].earliest_start, kv[0])
        ):
            device = self.devices[task.device_id]
            if device.kind == LoadKind.MUST_RUN and tid not in forced_map:
                place(
                    tid,
                    "planned",
                    start_bias=START_BIAS,
                    ignore_caps=True,
                    max_pauses_override=0,
                    hard_pauses=True,
                )
        rest = [
            t
            for t in self.tasks.values()
            if t.id not in forced_map
            and self.devices[t.device_id].kind != LoadKind.MUST_RUN
        ]
        rest.sort(key=lambda t: (t.latest_finish, -t.priority, t.id))
        for task in rest:
            place(task.id, "planned")

        # 2.5) 最迟完工统一核查（含锁定槽位；已锁定安排同样如实报告）
        for tid, task in self.tasks.items():
            future = [s for s in ctx.locked.get(tid, {}) if s >= now]
            if tid in placements:
                units, pl, _src = placements[tid]
                future += [
                    st + k for u, st in zip(units, pl.starts) for k in range(u.length)
                ]
            if not future:
                continue
            finish = max(future) + 1
            if finish > task.latest_finish:
                unmet.append(
                    UnmetConstraint(
                        constraint="latest_finish",
                        subject=tid,
                        task_id=tid,
                        window_id=self._window_of(finish - 1),
                        detail=(
                            f"任务 {tid} 预计完工槽位 {finish}，"
                            f"超出最迟完工 {finish - task.latest_finish} 个槽位"
                        ),
                    )
                )

        # 3) 削峰修复：把峰移出电网指令上限
        self._repair(ctx, placements, forced_map, unmet, sacrifices)

        # 3.5) 与无约束理想基线对比：记录每个任务为需量控制牺牲了什么
        self._record_ideal_sacrifices(ctx, placements, forced_map, sacrifices)

        # 4) 汇总输出
        return self._emit(ctx, placements, unmet, sacrifices)

    # ------------------------------------------------------------------
    # 剩余曲线展开
    # ------------------------------------------------------------------

    def _remaining_units(self, ctx: _Context, tid: str) -> list[_Unit]:
        """把任务未锁定部分的曲线展开为放置单元。

        已执行槽位与冻结槽位（保温锁/保护锁）都视为曲线已被消耗。
        """
        task = self.tasks[tid]
        locked = ctx.locked.get(tid, {})
        executed = sum(1 for rec in locked.values() if rec.source == "executed")
        frozen = sum(
            1 for s, rec in locked.items() if rec.source != "executed" and s >= ctx.now
        )
        done = executed + frozen
        scale = ctx.task_power_scale.get(tid, 1.0)
        device = self.devices[task.device_id]
        units: list[_Unit] = []
        acc = 0
        for idx, seg in enumerate(task.segments):
            remain = seg.duration_slots - max(0, min(seg.duration_slots, done - acc))
            acc += seg.duration_slots
            if remain <= 0:
                continue
            atomic = seg.kind != SegmentKind.NORMAL or device.kind != LoadKind.INTERRUPTIBLE
            if atomic:
                units.append(_Unit(idx, remain, seg.power_kw * scale))
            else:
                units.extend(_Unit(idx, 1, seg.power_kw * scale) for _ in range(remain))
        return units

    # ------------------------------------------------------------------
    # 单元放置成本与可行性
    # ------------------------------------------------------------------

    def _starts_in_day(self, ctx: _Context, device_id: str, day: int) -> int:
        """设备在自然日内的启停次数（off→on 沿），含暖机。"""
        spd = ctx.slots_per_day
        lo, hi = day * spd, min((day + 1) * spd, ctx.horizon)
        count = 0
        for t in range(lo, hi):
            if ctx.board.device_on(device_id, t) and not ctx.board.device_on(device_id, t - 1):
                count += 1
        return count

    def _unit_cost(
        self,
        ctx: _Context,
        task: TaskSpec,
        device: Device,
        unit: _Unit,
        start: int,
        *,
        is_new_block: bool,
        pauses_used: int,
        forbid_slots: frozenset[int],
        ignore_caps: bool,
    ) -> tuple[float, bool]:
        """返回 (成本, 是否可行)。硬约束不满足则不可行。"""
        if start < ctx.now or start + unit.length > ctx.horizon:
            return 0.0, False
        board = ctx.board
        cost = 0.0
        for k in range(unit.length):
            t = start + k
            # 不得与自身锁定槽位（已执行/保温/保护）双订
            if ctx.locked_has(task.id, t):
                return 0.0, False
            # 设备容量
            if board.device_kw[t].get(device.id, 0.0) + unit.kw > device.max_power_kw + EPS:
                return 0.0, False
            # 设备互锁：同组其他设备不得带载
            for other in self._interlocked[device.id]:
                if board.device_kw[t].get(other, 0.0) > EPS:
                    return 0.0, False
            # 电网指令上限（软）
            if not ignore_caps:
                excess = ctx.site_kw_at(t) + unit.kw - ctx.cap_at(t)
                if excess > 0:
                    cost += excess * NOTICE_EXCESS_PENALTY
            if t in forbid_slots:
                cost += FORBID_SLOT_PENALTY
            cost += unit.kw * ctx.slot_hours * ctx.prices.price_at(t)
        # 恢复爬坡：新运行块且设备上一槽位停机 → 需要暖机槽位
        if is_new_block and device.ramp_slots > 0 and not board.device_on(device.id, start - 1):
            for w in range(start - device.ramp_slots, start):
                if w < ctx.now or board.device_on(device.id, w):
                    return 0.0, False
                if not ignore_caps:
                    excess = ctx.site_kw_at(w) + device.ramp_power_kw - ctx.cap_at(w)
                    if excess > 0:
                        cost += excess * NOTICE_EXCESS_PENALTY
                cost += device.ramp_power_kw * ctx.slot_hours * ctx.prices.price_at(w)
        # 每日启停次数（硬，保守估计：本任务已产生的块都计入当天）
        if is_new_block and device.max_starts_per_day is not None:
            if not board.device_on(device.id, start - 1):
                day = start // ctx.slots_per_day
                used = self._starts_in_day(ctx, device.id, day)
                if used + pauses_used + 1 > device.max_starts_per_day:
                    return 0.0, False
        return cost, True

    def _dp_place(
        self,
        ctx: _Context,
        task: TaskSpec,
        device: Device,
        units: list[_Unit],
        *,
        fixed_start: Optional[int] = None,
        start_bias: float = 0.0,
        ignore_caps: bool = False,
        max_pauses_override: Optional[int] = None,
        hard_pauses: bool = False,
        forbid_slots: frozenset[int] = frozenset(),
    ) -> _Placement:
        """动态规划选择每个放置单元的起始槽位，最小化电费+罚分。"""
        if not units:
            return _Placement()
        max_pauses = task.max_pauses if max_pauses_override is None else max_pauses_override
        pause_slack = max_pauses if hard_pauses else max_pauses + 2
        earliest = max(task.earliest_start, ctx.now)

        # states[(last_end, pauses)] = (cost, starts_tuple)
        states: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {}
        u0 = units[0]
        if fixed_start is not None:
            first_starts: Any = [fixed_start]
        else:
            first_starts = range(earliest, ctx.horizon - u0.length + 1)
        for s in first_starts:
            new_block = not ctx.locked_has(task.id, s - 1)
            c, ok = self._unit_cost(
                ctx, task, device, u0, s,
                is_new_block=new_block, pauses_used=0,
                forbid_slots=forbid_slots, ignore_caps=ignore_caps,
            )
            if not ok:
                continue
            c += start_bias * s
            key = (s + u0.length, 0)
            if key not in states or states[key][0] > c:
                states[key] = (c, (s,))

        for i in range(1, len(units)):
            u = units[i]
            nxt: dict[tuple[int, int], tuple[float, tuple[int, ...]]] = {}
            for (last_end, p), (c0, path) in states.items():
                for s in range(last_end, ctx.horizon - u.length + 1):
                    gap = s > last_end
                    p2 = p + (1 if gap else 0)
                    if p2 > pause_slack:
                        continue
                    c, ok = self._unit_cost(
                        ctx, task, device, u, s,
                        is_new_block=gap, pauses_used=p2,
                        forbid_slots=forbid_slots, ignore_caps=ignore_caps,
                    )
                    if not ok:
                        continue
                    overflow = max(0, p2 - max_pauses)
                    total = c0 + c + overflow * PAUSE_OVERFLOW_PENALTY
                    key = (s + u.length, p2)
                    if key not in nxt or nxt[key][0] > total:
                        nxt[key] = (total, path + (s,))
            states = nxt
            if not states:
                break

        best: Optional[_Placement] = None
        best_key: Optional[tuple] = None
        for (last_end, p), (c0, path) in states.items():
            late = max(0, last_end - task.latest_finish)
            total = c0 + late * LATE_PENALTY
            # 确定性平局裁决：成本 → 中断次数 → 完工槽位 → 起始序列
            key = (total, p, last_end, path)
            if best_key is None or key < best_key:
                best_key = key
                best = _Placement(
                    starts=list(path),
                    cost=total,
                    pauses=p,
                    late_slots=late,
                    pause_overflow=max(0, p - max_pauses),
                )
        if best is not None:
            return best
        # 兜底：无视约束从最早位置连续放下，保证计划完整
        s0 = fixed_start if fixed_start is not None else earliest
        s0 = max(ctx.now, min(s0, ctx.horizon - sum(u.length for u in units)))
        starts: list[int] = []
        s = s0
        for u in units:
            starts.append(s)
            s += u.length
        return _Placement(
            starts=starts,
            cost=float("inf"),
            pauses=0,
            late_slots=max(0, s - task.latest_finish),
            infeasible=True,
        )

    def _warmup_slots(
        self,
        ctx: _Context,
        task: TaskSpec,
        device: Device,
        units: list[_Unit],
        placement: _Placement,
    ) -> list[int]:
        """确定该放置所需的暖机爬坡槽位（设备从停机恢复带载前）。"""
        if device.ramp_slots <= 0:
            return []
        board = ctx.board
        warm: list[int] = []
        occupied: set[int] = set()
        prev_end: Optional[int] = None
        for u, s in zip(units, placement.starts):
            if prev_end is None:
                new_block = not ctx.locked_has(task.id, s - 1)
            else:
                new_block = s > prev_end
            if new_block:
                on = board.device_on(device.id, s - 1) or (s - 1) in occupied
                if not on:
                    for w in range(s - device.ramp_slots, s):
                        if (
                            ctx.now <= w < ctx.horizon
                            and not board.device_on(device.id, w)
                            and w not in occupied
                        ):
                            warm.append(w)
                            occupied.add(w)
            occupied.update(range(s, s + u.length))
            prev_end = s + u.length
        return warm

    def _commit(
        self,
        ctx: _Context,
        task: TaskSpec,
        device: Device,
        units: list[_Unit],
        placement: _Placement,
    ) -> None:
        board = ctx.board
        placement.warmups = self._warmup_slots(ctx, task, device, units, placement)
        for w in placement.warmups:
            if 0 <= w < ctx.horizon:
                board.warmup_kw[w][device.id] = board.warmup_kw[w].get(device.id, 0.0) + device.ramp_power_kw
        for u, s in zip(units, placement.starts):
            for k in range(u.length):
                t = s + k
                if 0 <= t < ctx.horizon:
                    board.task_kw[t][task.id] = board.task_kw[t].get(task.id, 0.0) + u.kw
                    board.device_kw[t][device.id] = board.device_kw[t].get(device.id, 0.0) + u.kw

    def _uncommit(
        self,
        ctx: _Context,
        task: TaskSpec,
        device: Device,
        units: list[_Unit],
        placement: _Placement,
    ) -> None:
        board = ctx.board
        for w in placement.warmups:
            if not (0 <= w < ctx.horizon):
                continue
            cur = board.warmup_kw[w].get(device.id, 0.0) - device.ramp_power_kw
            if cur > EPS:
                board.warmup_kw[w][device.id] = cur
            else:
                board.warmup_kw[w].pop(device.id, None)
        placement.warmups = []
        for u, s in zip(units, placement.starts):
            for k in range(u.length):
                t = s + k
                if not (0 <= t < ctx.horizon):
                    continue
                board.task_kw[t].pop(task.id, None)
                left = board.device_kw[t].get(device.id, 0.0) - u.kw
                if left > EPS:
                    board.device_kw[t][device.id] = left
                else:
                    board.device_kw[t].pop(device.id, None)

    # ------------------------------------------------------------------
    # 削峰修复
    # ------------------------------------------------------------------

    def _repair(
        self,
        ctx: _Context,
        placements: dict[str, tuple[list[_Unit], _Placement, str]],
        forced_map: dict[str, int],
        unmet: list[UnmetConstraint],
        sacrifices: list[Sacrifice],
    ) -> None:
        board = ctx.board

        def over_slots() -> list[int]:
            return [
                t
                for t in range(ctx.now, ctx.horizon)
                if ctx.site_kw_at(t) > ctx.cap_at(t) + EPS
            ]

        def total_excess() -> float:
            return sum(ctx.site_kw_at(t) - ctx.cap_at(t) for t in over_slots())

        def movable(tid: str) -> bool:
            if tid in forced_map or tid not in placements:
                return False
            device = self.devices[self.tasks[tid].device_id]
            return device.kind in (LoadKind.SHIFTABLE, LoadKind.INTERRUPTIBLE, LoadKind.RAMPING)

        def energy_cost(units: list[_Unit], pl: _Placement) -> float:
            return sum(
                u.kw * ctx.slot_hours * ctx.prices.price_at(s + k)
                for u, s in zip(units, pl.starts)
                for k in range(u.length)
            )

        for _ in range(MAX_REPAIR_ITERS):
            overs = over_slots()
            if not overs:
                return
            before = total_excess()
            t_worst = max(overs, key=lambda t: ctx.site_kw_at(t) - ctx.cap_at(t))
            candidates = [
                tid
                for tid, kw in board.task_kw[t_worst].items()
                if kw > EPS and movable(tid)
            ]
            candidates.sort(key=lambda tid: -board.task_kw[t_worst][tid])
            improved = False
            for tid in candidates:
                task = self.tasks[tid]
                device = self.devices[task.device_id]
                units, old, source = placements[tid]
                old_energy = energy_cost(units, old)
                self._uncommit(ctx, task, device, units, old)
                forbid = frozenset(overs)
                new = self._dp_place(ctx, task, device, units, forbid_slots=forbid)
                self._commit(ctx, task, device, units, new)
                after = total_excess()
                if after < before - EPS and not new.infeasible:
                    placements[tid] = (units, new, source)
                    new_start = min(new.starts) if new.starts else None
                    old_start = min(old.starts) if old.starts else None
                    sacrifices.append(
                        Sacrifice(
                            kind="task_moved_for_cap",
                            task_id=tid,
                            window_id=self._window_of(t_worst),
                            detail=(
                                f"为满足电网指令上限，任务 {task.name}({tid}) "
                                f"从槽位 {old_start} 移至 {new_start}"
                                + (
                                    f"，多中断 {new.pauses - old.pauses} 次"
                                    if new.pauses > old.pauses
                                    else ""
                                )
                            ),
                            cost_delta=round(energy_cost(units, new) - old_energy, 4),
                            extra_pauses=max(0, new.pauses - old.pauses),
                            old_start=old_start,
                            new_start=new_start,
                        )
                    )
                    improved = True
                    break
                # 回滚
                self._uncommit(ctx, task, device, units, new)
                self._commit(ctx, task, device, units, old)
            if not improved:
                # 无法继续削峰：为仍越限的窗口记录未满足约束（逐槽位判定，
                # 只比较指令覆盖的槽位）
                for w in self.windows.values():
                    lo = max(w.start_slot, ctx.now)
                    hi = min(w.end_slot, ctx.horizon)
                    violated = [
                        t
                        for t in range(lo, hi)
                        if ctx.cap_at(t) != float("inf")
                        and ctx.site_kw_at(t) > ctx.cap_at(t) + EPS
                    ]
                    if not violated:
                        continue
                    peak_t = max(
                        violated, key=lambda t: ctx.site_kw_at(t) - ctx.cap_at(t)
                    )
                    cap = ctx.cap_at(peak_t)
                    peak = ctx.site_kw_at(peak_t)
                    blockers = sorted(
                        tid
                        for tid, kw in board.task_kw[peak_t].items()
                        if kw > EPS and not movable(tid)
                    )
                    unmet.append(
                        UnmetConstraint(
                            constraint="demand_cap",
                            subject=w.id,
                            window_id=w.id,
                            slot=peak_t,
                            detail=(
                                f"窗口 {w.id} 槽位 {peak_t} 需量 {peak:.1f}kW 仍超电网指令上限 "
                                f"{cap:.1f}kW；该处不可移负荷: {blockers or '无'}"
                            ),
                        )
                    )
                # 不属于任何计费窗口的越限槽位也要如实记录
                leftover = [t for t in over_slots() if self._window_of(t) is None]
                if leftover:
                    peak_t = max(leftover, key=lambda t: ctx.site_kw_at(t) - ctx.cap_at(t))
                    blockers = sorted(
                        tid
                        for tid, kw in board.task_kw[peak_t].items()
                        if kw > EPS and not movable(tid)
                    )
                    unmet.append(
                        UnmetConstraint(
                            constraint="demand_cap",
                            subject="site",
                            slot=peak_t,
                            detail=(
                                f"槽位 {peak_t} 需量 {ctx.site_kw_at(peak_t):.1f}kW 仍超电网指令上限 "
                                f"{ctx.cap_at(peak_t):.1f}kW；该处不可移负荷: {blockers or '无'}"
                            ),
                        )
                    )
                return

    def _window_of(self, slot: int) -> Optional[str]:
        for w in self.windows.values():
            if w.start_slot <= slot < w.end_slot:
                return w.id
        return None

    # ------------------------------------------------------------------
    # 削减代价（理想基线对比）
    # ------------------------------------------------------------------

    def _record_ideal_sacrifices(
        self,
        ctx: _Context,
        placements: dict[str, tuple[list[_Unit], _Placement, str]],
        forced_map: dict[str, int],
        sacrifices: list[Sacrifice],
    ) -> None:
        """把每个任务的最终放置与"无电网指令、无其他负荷"的理想放置对比，
        差异即为本次需量控制的削减代价（移峰、增停、晚完工、电费增加）。"""
        if not ctx.caps:
            return
        repaired = {s.task_id for s in sacrifices}
        for tid, (units, pl, source) in placements.items():
            if tid in repaired or source == "forced" or pl.infeasible or not units:
                continue
            task = self.tasks[tid]
            device = self.devices[task.device_id]
            if device.kind == LoadKind.MUST_RUN:
                continue
            # 理想世界：本任务独占设备、无指令上限
            board = _Board(ctx.horizon)
            for s, rec in ctx.locked.get(tid, {}).items():
                if 0 <= s < ctx.horizon:
                    board.task_kw[s][tid] = board.task_kw[s].get(tid, 0.0) + rec.kw
                    board.device_kw[s][device.id] = (
                        board.device_kw[s].get(device.id, 0.0) + rec.kw
                    )
            ideal_ctx = _Context(
                now=ctx.now,
                horizon=ctx.horizon,
                slot_hours=ctx.slot_hours,
                slots_per_day=ctx.slots_per_day,
                board=board,
                base_load=ctx.base_load,
                prices=ctx.prices,
                caps={},
                locked=ctx.locked,
                task_power_scale=ctx.task_power_scale,
            )
            ideal = self._dp_place(ideal_ctx, task, device, units)
            if ideal.infeasible or not ideal.starts:
                continue
            if ideal.starts == pl.starts and ideal.pauses == pl.pauses:
                continue

            def energy(starts: list[int]) -> float:
                return sum(
                    u.kw * ctx.slot_hours * ctx.prices.price_at(s + k)
                    for u, s in zip(units, starts)
                    for k in range(u.length)
                )

            affected = [
                s + k
                for u, s in zip(units, ideal.starts)
                for k in range(u.length)
                if s + k in ctx.caps
            ]
            finish = max(s + u.length for u, s in zip(units, pl.starts))
            sacrifices.append(
                Sacrifice(
                    kind="shifted_for_demand",
                    task_id=tid,
                    window_id=self._window_of(min(affected)) if affected else None,
                    detail=(
                        f"需量控制使任务 {task.name}({tid}) 从槽位 {min(ideal.starts)} "
                        f"移至 {min(pl.starts)}"
                        + (
                            f"，多中断 {pl.pauses - ideal.pauses} 次"
                            if pl.pauses > ideal.pauses
                            else ""
                        )
                        + (
                            f"，晚完工 {finish - task.latest_finish} 槽位"
                            if finish > task.latest_finish
                            else ""
                        )
                    ),
                    cost_delta=round(energy(pl.starts) - energy(ideal.starts), 4),
                    extra_pauses=max(0, pl.pauses - ideal.pauses),
                    old_start=min(ideal.starts),
                    new_start=min(pl.starts),
                )
            )

    # ------------------------------------------------------------------
    # 输出汇总
    # ------------------------------------------------------------------

    def _emit(
        self,
        ctx: _Context,
        placements: dict[str, tuple[list[_Unit], _Placement, str]],
        unmet: list[UnmetConstraint],
        sacrifices: list[Sacrifice],
    ) -> ScheduleOutput:
        board = ctx.board
        assignments: dict[str, dict[int, ScheduledSlot]] = {
            tid: dict(slots) for tid, slots in ctx.locked.items()
        }
        for tid, (units, pl, source) in placements.items():
            task_slots = assignments.setdefault(tid, {})
            for u, s in zip(units, pl.starts):
                for k in range(u.length):
                    t = s + k
                    # 锁定槽位（已执行/保温/保护）不可被覆盖
                    if 0 <= t < ctx.horizon and t not in task_slots:
                        task_slots[t] = ScheduledSlot(kw=u.kw, seg_idx=u.seg_idx, source=source)
        warmups: dict[str, dict[int, float]] = {}
        for t in range(ctx.horizon):
            for dev, kw in board.warmup_kw[t].items():
                if kw > EPS:
                    warmups.setdefault(dev, {})[t] = kw

        site_kw = [ctx.site_kw_at(t) for t in range(ctx.horizon)]
        windows: dict[str, WindowAnalysis] = {}
        total_energy = 0.0
        total_penalty = 0.0
        for w in self.windows.values():
            lo, hi = max(w.start_slot, 0), min(w.end_slot, ctx.horizon)
            if lo >= hi:
                continue
            peak_t = max(range(lo, hi), key=lambda t: site_kw[t])
            peak = site_kw[peak_t]
            contributions: list[dict[str, Any]] = []
            base = ctx.base_load.get(peak_t, 0.0)
            if base > EPS:
                contributions.append({"source": "base", "kw": base})
            for tid, kw in board.task_kw[peak_t].items():
                if kw > EPS:
                    contributions.append({"source": tid, "kw": kw})
            for dev, kw in board.warmup_kw[peak_t].items():
                if kw > EPS:
                    contributions.append({"source": f"warmup:{dev}", "kw": kw})
            contributions.sort(key=lambda c: -c["kw"])
            energy_kwh = sum(site_kw[t] * ctx.slot_hours for t in range(lo, hi))
            energy_cost = sum(
                site_kw[t] * ctx.slot_hours * ctx.prices.price_at(t) for t in range(lo, hi)
            )
            caps = [
                {"slot": t, "max_demand_kw": ctx.cap_at(t)}
                for t in range(lo, hi)
                if ctx.cap_at(t) != float("inf")
            ]
            cap_exceeded = any(
                site_kw[t] > ctx.cap_at(t) + EPS
                for t in range(lo, hi)
                if ctx.cap_at(t) != float("inf")
            )
            penalty = max(0.0, peak - w.contract_kw) * w.penalty_per_kw
            total_energy += energy_cost
            total_penalty += penalty
            windows[w.id] = WindowAnalysis(
                window_id=w.id,
                start_slot=w.start_slot,
                end_slot=w.end_slot,
                contract_kw=w.contract_kw,
                caps=caps,
                peak_kw=peak,
                peak_slot=peak_t,
                contributions=contributions,
                energy_kwh=energy_kwh,
                energy_cost=energy_cost,
                demand_penalty=penalty,
                cap_exceeded=cap_exceeded,
                over_contract=peak > w.contract_kw + EPS,
            )
        return ScheduleOutput(
            assignments=assignments,
            warmups=warmups,
            site_kw=site_kw,
            windows=windows,
            total_energy_cost=total_energy,
            total_demand_penalty=total_penalty,
            total_cost=total_energy + total_penalty,
            sacrifices=sacrifices,
            unmet=unmet,
        )
