"""协同服务验收测试：滚动重算、约束、合并、仲裁、跨窗口查询与重启持久性。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app import timegrid as tg
from app.models import (
    BillingWindow,
    GridDirective,
    PowerCurvePoint,
    TariffSchedule,
    TariffSegment,
    Task,
)
from app.repository import Warehouse
from app.scenario import build_directive, build_service, build_tariff, build_windows
from app.service import CoordinationService

DAY0 = "2026-09-19T08:00"


def make_warehouse(path=None, tasks=None, windows=None, slots=18):
    wh = Warehouse(path)
    wh.configure(
        tariff=build_tariff(),
        windows=windows or build_windows(DAY0),
        horizon_start=tg.iso(tg.parse(DAY0).replace(hour=8)),
        horizon_slots=slots,
        clock_start=DAY0,
    )
    for task in (tasks or []):
        wh.upsert_task(task)
    return wh


def flat_task(task_id, name, kind, kw, es, lf, minutes, **kwarg):
    return Task(
        task_id=task_id, name=name, kind=kind,
        power_curve=[PowerCurvePoint(0, kw)],
        earliest_start=es, latest_finish=lf, duration_minutes=minutes, **kwarg
    )


class BaselinePlanningTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.svc.recalculate(reason="baseline")

    def test_baseline_peak_attribution(self):
        plan = self.svc.latest_plan()
        # 08:45 槽：热处理炉 450 + 压铸 260 + 空压机1 220 + 风机 120 + 基础 130 = 1180
        self.assertEqual(plan["total_kw"][3], 1180.0)
        peak = next(p for p in plan["peaks"] if p["range"] == "W02")
        self.assertEqual(peak["peak_kw"], 1180.0)
        top = peak["attribution"][0]
        self.assertEqual(top["task_id"], "furnace_a")
        self.assertEqual(top["kw"], 450.0)

    def test_baseline_violates_contract_cost_money(self):
        plan = self.svc.latest_plan()
        w02 = next(w for w in plan["windows"] if w["window_id"] == "W02")
        self.assertGreater(w02["baseline_demand_kw"], w02["contracted_kw"])
        self.assertGreater(w02["penalty"], 0)


class DirectiveResponseTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.svc.recalculate(reason="baseline")
        self.svc.receive_grid_notice(build_directive(DAY0, 0))

    def test_window_within_limit_after_response(self):
        plan = self.svc.latest_plan()
        for i in range(4, 10):  # 09:00-10:30
            self.assertLessEqual(plan["total_kw"][i], 700.0 + 1e-6)

    def test_sacrifice_explains_what_is_lost(self):
        plan = self.svc.latest_plan()
        self.assertTrue(plan["sacrifices"])
        sac = plan["sacrifices"][0]
        self.assertIn(sac["action"], ("shift", "interrupt_resume"))
        self.assertGreater(sac["kwh"], 0)
        if sac["action"] == "shift":
            self.assertGreaterEqual(sac["delay_minutes"], 0)
        else:
            self.assertIn("恢复", sac["detail"])
            self.assertIn("爬坡", sac["detail"])

    def test_ramping_interrupt_sacrifices_ramp_recovery(self):
        tasks = [
            flat_task("fan", "风机", "ramping", 300,
                      tg.iso(tg.parse(DAY0).replace(hour=8)),
                      tg.iso(tg.parse(DAY0).replace(hour=12)), 240,
                      ramp_recover_minutes=30, min_run_minutes=15, max_starts=3),
            flat_task("base", "基础", "must_run", 500,
                      tg.iso(tg.parse(DAY0).replace(hour=8)),
                      tg.iso(tg.parse(DAY0).replace(hour=12)), 240),
        ]
        wh = make_warehouse(tasks=tasks)
        svc = CoordinationService(wh)
        svc.recalculate()
        svc.receive_grid_notice(GridDirective(
            "N5", tg.iso(tg.parse(DAY0).replace(hour=8, minute=15)),
            tg.iso(tg.parse(DAY0).replace(hour=10)), 600.0,
            tg.iso(tg.parse(DAY0).replace(hour=7, minute=50))))
        plan = svc.latest_plan()
        sac = next(s for s in plan["sacrifices"] if s["task_id"] == "fan")
        self.assertEqual(sac["action"], "interrupt_resume")
        self.assertIn("爬坡 30 分钟", sac["detail"])
        # 恢复爬坡槽功率折减：10:00 恢复时 1/3 功率
        fan = next(t for t in plan["tasks"] if t["task_id"] == "fan")
        self.assertEqual(fan["kw"][8], 100.0)
        self.assertEqual(fan["kw"][9], 200.0)
        self.assertEqual(fan["kw"][10], 300.0)

    def test_holding_and_non_interruptible_segments_untouched(self):
        plan = self.svc.latest_plan()
        furnace = next(t for t in plan["tasks"] if t["task_id"] == "furnace_a")
        # 08:00-09:00 升温槽保持 450
        self.assertEqual(furnace["kw"][0:4], [450.0] * 4)
        # 09:00 后保温 80
        self.assertEqual(furnace["kw"][4], 80.0)

    def test_retry_same_notice_merges_single_decision(self):
        versions_before = len(self.svc.wh.plans)
        result = self.svc.receive_grid_notice(build_directive(DAY0, 7))
        self.assertTrue(result["merged"])
        self.assertEqual(len(self.svc.wh.plans), versions_before)
        plan = self.svc.latest_plan()
        self.assertIn(7, plan["merged_retry_seqs"])

    def test_changed_directive_parameters_trigger_new_version(self):
        d = build_directive(DAY0, 1)
        d.demand_limit_kw = 600.0
        result = self.svc.receive_grid_notice(d)
        self.assertFalse(result["merged"])
        self.assertGreater(result["plan_version"], 2)

    def test_plan_versions_are_comparable(self):
        cmp_ = self.svc.compare(1, 2)
        changed_ids = [c["task_id"] for c in cmp_["changed_tasks"]]
        self.assertTrue(changed_ids)
        self.assertIn("die_cast", changed_ids)
        self.assertLessEqual(cmp_["peak_to_kw"], cmp_["peak_from_kw"] + 1e-9)


class ConstraintEnforcementTest(unittest.TestCase):
    def test_latest_finish_bounds_shift(self):
        tasks = [
            flat_task("dc", "压铸", "shiftable", 300,
                      tg.iso(tg.parse(DAY0).replace(hour=8)),
                      tg.iso(tg.parse(DAY0).replace(hour=9)), 60),
            flat_task("base", "基础", "must_run", 500,
                      tg.iso(tg.parse(DAY0).replace(hour=8)),
                      tg.iso(tg.parse(DAY0).replace(hour=12, minute=30)), 270),
        ]
        wh = make_warehouse(tasks=tasks)
        svc = CoordinationService(wh)
        svc.recalculate()
        svc.receive_grid_notice(GridDirective(
            "N1", tg.iso(tg.parse(DAY0).replace(hour=8)),
            tg.iso(tg.parse(DAY0).replace(hour=10)), 600.0,
            tg.iso(tg.parse(DAY0).replace(hour=7, minute=50))))
        plan = svc.latest_plan()
        # 压铸最迟 09:00 完工，08:00-08:45 又是限值窗口且无更早落点 -> 无法移峰
        dc = next(t for t in plan["tasks"] if t["task_id"] == "dc")
        self.assertEqual(dc["kw"][0:4], [300.0] * 4)
        self.assertTrue(any(v["code"] == "demand_limit" for v in plan["violations"]))

    def test_max_starts_enforced(self):
        # 可中断负荷 300，最多 1 次额外启停，且 10:30-11:00 有工艺不可中断段：
        # 第一限值窗中断并恢复（用完启停额度）；第二限值窗无法再中断（不可尾部削减）
        ni_start = tg.iso(tg.parse(DAY0).replace(hour=10, minute=30))
        ni_end = tg.iso(tg.parse(DAY0).replace(hour=11))
        tasks = [
            flat_task("c", "可中断机", "interruptible", 300,
                      tg.iso(tg.parse(DAY0).replace(hour=8)),
                      tg.iso(tg.parse(DAY0).replace(hour=12, minute=30)), 270,
                      min_run_minutes=30, max_starts=1, starts_used=0,
                      non_interruptible=[(ni_start, ni_end)]),
            flat_task("base", "基础", "must_run", 200,
                      tg.iso(tg.parse(DAY0).replace(hour=8)),
                      tg.iso(tg.parse(DAY0).replace(hour=12, minute=30)), 270),
        ]
        wh = make_warehouse(tasks=tasks)
        svc = CoordinationService(wh)
        svc.recalculate()
        svc.receive_grid_notice(GridDirective(
            "N2a", tg.iso(tg.parse(DAY0).replace(hour=8, minute=30)),
            tg.iso(tg.parse(DAY0).replace(hour=9, minute=30)), 200.0,
            tg.iso(tg.parse(DAY0).replace(hour=7, minute=50))))
        svc.receive_grid_notice(GridDirective(
            "N2b", tg.iso(tg.parse(DAY0).replace(hour=10)),
            tg.iso(tg.parse(DAY0).replace(hour=12, minute=30)), 200.0,
            tg.iso(tg.parse(DAY0).replace(hour=9, minute=30))))
        plan = svc.latest_plan()
        self.assertTrue(any(v["code"] == "demand_limit" for v in plan["violations"]))
        c = next(t for t in plan["tasks"] if t["task_id"] == "c")
        # 第一窗口四槽中断，10:00 恢复
        self.assertEqual(c["kw"][2:6], [0.0] * 4)
        self.assertEqual(c["kw"][6:8], [300.0, 300.0])
        # 第二窗口因启停额度已尽保持运行，工艺不可中断段也保持
        self.assertEqual(c["kw"][8:10], [300.0, 300.0])
        self.assertEqual(c["kw"][10:12], [300.0, 300.0])

    def test_interlock_never_co_schedules(self):
        tasks = [
            flat_task("a", "甲机", "interruptible", 100,
                      tg.iso(tg.parse(DAY0).replace(hour=8)),
                      tg.iso(tg.parse(DAY0).replace(hour=9)), 60,
                      min_run_minutes=15, max_starts=3, interlock_group="g"),
            flat_task("b", "乙机", "interruptible", 100,
                      tg.iso(tg.parse(DAY0).replace(hour=8, minute=30)),
                      tg.iso(tg.parse(DAY0).replace(hour=9, minute=30)), 60,
                      min_run_minutes=15, max_starts=3, interlock_group="g"),
        ]
        wh = make_warehouse(tasks=tasks)
        svc = CoordinationService(wh)
        svc.recalculate()
        # 限值 100：互锁组内同一时刻至多一台运行，调度器必须轮流中断
        svc.receive_grid_notice(GridDirective(
            "N4", tg.iso(tg.parse(DAY0).replace(hour=8, minute=45)),
            tg.iso(tg.parse(DAY0).replace(hour=9, minute=30)), 100.0,
            tg.iso(tg.parse(DAY0).replace(hour=7, minute=50))))
        plan = svc.latest_plan()
        self.assertEqual([], plan["violations"])
        for i in range(3, 6):
            ka = next(t for t in plan["tasks"] if t["task_id"] == "a")["kw"][i]
            kb = next(t for t in plan["tasks"] if t["task_id"] == "b")["kw"][i]
            self.assertFalse(ka > 0 and kb > 0, f"槽 {i} 互锁失效")

    def test_ramp_recovery_respects_latest_finish(self):
        tasks = [
            flat_task("f", "风机", "ramping", 300,
                      tg.iso(tg.parse(DAY0).replace(hour=12)),
                      tg.iso(tg.parse(DAY0).replace(hour=12, minute=15)), 15,
                      ramp_recover_minutes=30, min_run_minutes=15, max_starts=3),
            flat_task("base", "基础", "must_run", 500,
                      tg.iso(tg.parse(DAY0).replace(hour=8)),
                      tg.iso(tg.parse(DAY0).replace(hour=12, minute=30)), 270),
        ]
        wh = make_warehouse(tasks=tasks)
        svc = CoordinationService(wh)
        svc.recalculate()
        svc.receive_grid_notice(GridDirective(
            "N3", tg.iso(tg.parse(DAY0).replace(hour=8)),
            tg.iso(tg.parse(DAY0).replace(hour=12, minute=30)), 600.0,
            tg.iso(tg.parse(DAY0).replace(hour=7, minute=50))))
        plan = svc.latest_plan()
        # 12:00 后无恢复空间，不可中断 -> 该槽超限登记，而不是违规中断
        f = next(t for t in plan["tasks"] if t["task_id"] == "f")
        self.assertEqual(f["kw"][16], 300.0)


class ProtectionArbitrationTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.svc.recalculate()
        self.svc.receive_grid_notice(build_directive(DAY0, 0))

    def _protect_all_levers(self):
        self.svc.protect_task("fan_ramp", 150, "工件需要风量")
        self.svc.protect_task("compressor_1", 150, "气源不可断")
        r = self.svc.protect_task("die_cast", 150, "批次不可移")
        return r

    def test_conflicting_protection_holds_for_arbitration(self):
        r = self._protect_all_levers()
        self.assertEqual(r["state"], "awaiting_arbitration")
        with self.assertRaises(PermissionError):
            self.svc.approve()

    def test_arbitration_energy_priority_unblocks(self):
        r = self._protect_all_levers()
        for c in self.svc.latest_plan()["protection_conflicts"]:
            self.svc.arbitrate(c["protection_id"], "energy_priority", by="厂长")
        plan = self.svc.latest_plan()
        self.assertEqual(plan["state"], "calculated")
        self.assertEqual([v["code"] for v in plan["violations"]], [])
        approved = self.svc.approve()
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(approved["approved_by"], "energy_manager")
        # 首个槽位按批准版本落地后，滚动新版本接替，旧版本收口为 closed
        self.svc.advance(15)
        old = self.svc.wh.plan_by_version(approved["version"])
        self.assertEqual(old["state"], "closed")
        frozen = self.svc.wh.actual_at("2026-09-19T08:00")
        self.assertIsNotNone(frozen)

    def test_arbitration_production_protected_keeps_violation(self):
        self._protect_all_levers()
        for c in self.svc.latest_plan()["protection_conflicts"]:
            self.svc.arbitrate(c["protection_id"], "production_protected", by="厂长")
        plan = self.svc.latest_plan()
        self.assertTrue(any(v["code"] == "demand_limit" for v in plan["violations"]))
        with self.assertRaises(PermissionError):
            self.svc.approve()

    def test_time_limited_protection_expires(self):
        self._protect_all_levers()
        for c in self.svc.latest_plan()["protection_conflicts"]:
            self.svc.arbitrate(c["protection_id"], "time_limited_exception", by="厂长")
        # 保护窗口内仍冲突超限
        self.assertTrue(any(v["code"] == "demand_limit"
                            for v in self.svc.latest_plan()["violations"]))
        # 越过 150 分钟保护期后，任务重新可调度，限值窗口只剩 09:00-10:30 已过 -> 无冲突
        self.svc.advance(180)
        self.assertEqual(
            [v["code"] for v in self.svc.latest_plan()["violations"]], [])


class RollingSimulationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".json")
        self.svc = build_service(self.tmp)
        self.svc.recalculate()
        self.svc.receive_grid_notice(build_directive(DAY0, 0))
        self.svc.approve()

    def tearDown(self):
        Path(self.tmp).unlink(missing_ok=True)

    def test_advance_freezes_actuals_and_snapshots(self):
        # 08:30 槽注入 +20kW 现场偏差
        self.svc.advance(120, meter_overrides={30: {"base_load": 150}})
        plan = self.svc.latest_plan()
        closed = [w for w in plan["windows"] if w["actual_demand_kw"] is not None]
        self.assertEqual(len(closed), 4)
        w01 = next(w for w in plan["windows"] if w["window_id"] == "W01")
        self.assertEqual(w01["actual_demand_kw"] - w01["planned_demand_kw"], 20.0)
        self.assertIsNotNone(w01["executed_version"])
        # W02 实际需量 920 > 契约 900，产生真实超约罚金
        w02 = next(w for w in plan["windows"] if w["window_id"] == "W02")
        self.assertGreater(w02["actual_penalty"], 0)
        self.assertEqual(w02["actual_penalty"], (920 - 900) * 120)

    def test_past_usage_immutable_after_restart(self):
        self.svc.advance(60)
        executed = self.svc.latest_plan()
        past_before = {t["task_id"]: t["kw"][:4] for t in executed["tasks"]}
        wh2 = Warehouse(self.tmp)
        svc2 = CoordinationService(wh2)
        new_plan = svc2.recalculate(reason="重启滚动")
        for tid, series in past_before.items():
            view = next(t for t in new_plan["tasks"] if t["task_id"] == tid)
            self.assertEqual(view["kw"][:4], series)
        # 后续再推进，关闭窗口的计划快照不变
        svc2.advance(60)
        p3 = svc2.latest_plan()
        w01 = next(w for w in p3["windows"] if w["window_id"] == "W01")
        self.assertEqual(w01["planned_demand_kw"],
                         executed["windows"][0]["planned_demand_kw"])

    def test_rewrite_frozen_slot_rejected(self):
        self.svc.advance(30)
        with self.assertRaises(PermissionError):
            self.svc.wh.record_actuals({
                "2026-09-19T08:00": {"furnace_a": 1.0},
            })

    def test_forecast_deviation_triggers_revision(self):
        self.svc.advance(60)
        versions = len(self.svc.wh.plans)
        self.svc.report_forecast_deviation({"compressor_1": 40})
        self.assertGreater(len(self.svc.wh.plans), versions)

    def test_force_run_becomes_non_interruptible(self):
        self.svc.force_run("die_cast",
                           tg.iso(tg.parse(DAY0).replace(hour=9)),
                           tg.iso(tg.parse(DAY0).replace(hour=11)))
        plan = self.svc.latest_plan()
        dc = next(t for t in plan["tasks"] if t["task_id"] == "die_cast")
        self.assertEqual(dc["kw"][4:12], [260.0] * 8)

    def test_cross_window_dashboard(self):
        self.svc.advance(120, meter_overrides={30: {"base_load": 150}})
        dash = self.svc.dashboard()
        self.assertEqual(dash["now"], "2026-09-19T10:00:00+08:00")
        self.assertEqual(len([w for w in dash["windows"] if w["actual_demand_kw"] is not None]), 4)
        self.assertTrue(any(w["demand_delta_kw"] == 20.0 for w in dash["windows"]))
        self.assertIn("pending_arbitration", dash)
        self.assertGreater(dash["cost"]["planned_all_windows"], 0)


if __name__ == "__main__":
    unittest.main()
