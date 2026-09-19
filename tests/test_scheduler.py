"""滚动调度引擎的约束行为测试。"""

import json
import unittest
from pathlib import Path

from app.models import (
    ArbitrationResult,
    DemandWindow,
    Device,
    GridNotice,
    LoadKind,
    PlanState,
    SegmentKind,
    SegmentSpec,
    Tariff,
    TariffPeriod,
    TaskSpec,
)
from app.scheduler import RollingScheduler

FLAT = Tariff(periods=[TariffPeriod(0, 10_000, 1.0)])
HORIZON = 96


def make_scheduler(devices, tasks, windows=None, tariff=None):
    return RollingScheduler(
        slot_hours=0.25,
        slots_per_day=96,
        devices={d.id: d for d in devices},
        tasks={t.id: t for t in tasks},
        tariff=tariff or FLAT,
        windows={w.id: w for w in (windows or [])},
    )


def plan(sched, **kwargs):
    defaults = dict(
        now=0,
        horizon=HORIZON,
        locked={},
        forced=[],
        notices=[],
        base_load={},
    )
    defaults.update(kwargs)
    return sched.plan(**defaults)


class ContractConsistencyTest(unittest.TestCase):
    """代码枚举与 domain_contract.json 保持一致。"""

    def test_enums_match_contract(self):
        contract = json.loads(Path("domain_contract.json").read_text(encoding="utf-8"))
        self.assertEqual({k.value for k in LoadKind}, set(contract["load_kinds"]))
        self.assertEqual({s.value for s in PlanState}, set(contract["plan_states"]))
        self.assertEqual(
            {r.value for r in ArbitrationResult}, set(contract["arbitration_results"])
        )


class UninterruptibleTest(unittest.TestCase):
    def test_uninterruptible_segment_stays_contiguous(self):
        dev = Device("d1", "炉", LoadKind.INTERRUPTIBLE, max_power_kw=600)
        task = TaskSpec(
            "A",
            "d1",
            "热处理",
            segments=[
                SegmentSpec(2, 200, SegmentKind.NORMAL),
                SegmentSpec(3, 500, SegmentKind.UNINTERRUPTIBLE),
            ],
            earliest_start=0,
            latest_finish=20,
            max_pauses=3,
        )
        sched = make_scheduler([dev], [task])
        notice = GridNotice("n1", 0, 10, max_demand_kw=400)
        out = plan(sched, notices=[notice])
        slots = out.assignments["A"]
        hot = sorted(s for s, rec in slots.items() if rec.kw == 500)
        self.assertEqual(len(hot), 3)
        # 不可中断段必须连续，且整体避开指令上限无法容纳它的区域
        self.assertEqual(hot, list(range(hot[0], hot[0] + 3)))
        self.assertGreaterEqual(hot[0], 10)


class InterlockTest(unittest.TestCase):
    def test_interlocked_devices_never_overlap(self):
        d1 = Device("d1", "炉", LoadKind.SHIFTABLE, 500, interlock_group="g")
        d2 = Device("d2", "机", LoadKind.SHIFTABLE, 500, interlock_group="g")
        t1 = TaskSpec("A", "d1", "a", [SegmentSpec(4, 300)], 0, 12)
        t2 = TaskSpec("B", "d2", "b", [SegmentSpec(4, 300)], 0, 12)
        sched = make_scheduler([d1, d2], [t1, t2])
        out = plan(sched)
        for t in range(HORIZON):
            a = out.assignments["A"].get(t)
            b = out.assignments["B"].get(t)
            self.assertFalse(
                a and b and a.kw > 0 and b.kw > 0,
                f"槽位 {t} 互锁设备同时带载",
            )
        self.assertEqual(len(out.assignments["A"]), 4)
        self.assertEqual(len(out.assignments["B"]), 4)


class RampTest(unittest.TestCase):
    def test_recovery_ramp_requires_warmup_slots(self):
        dev = Device(
            "d1", "炉", LoadKind.RAMPING, 500, ramp_slots=2, ramp_power_kw=100
        )
        task = TaskSpec("A", "d1", "a", [SegmentSpec(3, 400)], 0, 20)
        sched = make_scheduler([dev], [task])
        out = plan(sched)
        starts = sorted(out.assignments["A"])
        # 设备停机，恢复带载前需要 2 个暖机槽位 → 任务不能从 0 开始
        self.assertEqual(starts[0], 2)
        self.assertEqual(out.warmups.get("d1"), {0: 100, 1: 100})


class StartsLimitTest(unittest.TestCase):
    def test_max_starts_per_day_blocks_split(self):
        dev = Device("d1", "机", LoadKind.INTERRUPTIBLE, 300, max_starts_per_day=1)
        task = TaskSpec(
            "A", "d1", "a", [SegmentSpec(4, 200)], 0, 20, max_pauses=2
        )
        sched = make_scheduler([dev], [task])
        # 指令上限 150 迫使任务避开槽位 2-6，若拆分需 2 次启动 > 每日 1 次
        notice = GridNotice("n1", 2, 6, max_demand_kw=150)
        out = plan(sched, notices=[notice])
        slots = sorted(out.assignments["A"])
        self.assertEqual(slots, list(range(slots[0], slots[0] + 4)))


class DeadlineAndTouTest(unittest.TestCase):
    def test_shiftable_task_prefers_valley_and_meets_deadline(self):
        tariff = Tariff(
            periods=[
                TariffPeriod(0, 10, 0.4),
                TariffPeriod(10, HORIZON, 1.0),
            ]
        )
        dev = Device("d1", "机", LoadKind.SHIFTABLE, 200)
        task = TaskSpec("A", "d1", "a", [SegmentSpec(3, 100)], 0, 50)
        sched = make_scheduler([dev], [task], tariff=tariff)
        out = plan(sched)
        slots = sorted(out.assignments["A"])
        self.assertEqual(slots, [0, 1, 2])  # 全部落在谷段
        self.assertLessEqual(slots[-1] + 1, task.latest_finish)


class DemandCapRepairTest(unittest.TestCase):
    def test_unmeetable_cap_is_reported_not_hidden(self):
        d1 = Device("d1", "炉", LoadKind.MUST_RUN, 500)
        t1 = TaskSpec("A", "d1", "a", [SegmentSpec(4, 400)], 2, 20)
        sched = make_scheduler([d1], [t1])
        # must_run 400kW 必然越过 300kW 的指令上限 → 记录未满足约束
        notice = GridNotice("n1", 0, 10, max_demand_kw=300)
        out = plan(sched, notices=[notice])
        kinds = {u.constraint for u in out.unmet}
        self.assertIn("demand_cap", kinds)


if __name__ == "__main__":
    unittest.main()
