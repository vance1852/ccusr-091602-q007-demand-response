"""制造负荷与生产承诺协同服务的验收场景测试。

场景：园区接到电网临时需量控制通知，热处理炉（不可中断+保温）、
空压机（可中断）、输送线（must_run）、制冷机（可移峰）需要滚动重排。
"""

import tempfile
import unittest
from pathlib import Path

from app import (
    ArbitrationResult,
    CoordinationService,
    DemandWindow,
    Device,
    ForecastDeviation,
    ForcedRun,
    GridNotice,
    LoadKind,
    PlanState,
    ProtectionStatus,
    SegmentKind,
    SegmentSpec,
    ServiceConfig,
    Tariff,
    TariffPeriod,
    TaskSpec,
)

EPOCH = "2026-09-19T00:00:00+08:00"


def build_service(storage_path=None) -> CoordinationService:
    config = ServiceConfig(epoch_iso=EPOCH, slot_minutes=15, horizon_slots=96)
    tariff = Tariff(
        periods=[
            TariffPeriod(0, 32, 0.4),    # 谷
            TariffPeriod(32, 48, 1.2),   # 峰 08:00-12:00
            TariffPeriod(48, 96, 0.8),   # 平
        ]
    )
    windows = [
        DemandWindow("W1", 32, 48, contract_kw=2000, penalty_per_kw=100),
        DemandWindow("W2", 48, 72, contract_kw=2000, penalty_per_kw=100),
        DemandWindow("W3", 72, 96, contract_kw=2000, penalty_per_kw=100),
    ]
    devices = [
        Device("furnace", "热处理炉", LoadKind.RAMPING, 800,
               interlock_group="g1", ramp_slots=2, ramp_power_kw=150),
        Device("compressor", "空压机", LoadKind.INTERRUPTIBLE, 500,
               interlock_group="g1"),
        Device("conveyor", "输送线", LoadKind.MUST_RUN, 300),
        Device("chiller", "制冷机", LoadKind.SHIFTABLE, 400),
    ]
    tasks = [
        TaskSpec("T1", "furnace", "热处理", [
            SegmentSpec(4, 600, SegmentKind.UNINTERRUPTIBLE),  # 升温不可中断
            SegmentSpec(6, 300, SegmentKind.HOLDING),          # 保温
            SegmentSpec(2, 100, SegmentKind.NORMAL),           # 冷却
        ], earliest_start=34, latest_finish=50),
        TaskSpec("T2", "compressor", "空压", [
            SegmentSpec(8, 350, SegmentKind.NORMAL),
        ], earliest_start=32, latest_finish=88, max_pauses=2),
        TaskSpec("T3", "conveyor", "输送", [
            SegmentSpec(6, 200, SegmentKind.NORMAL),
        ], earliest_start=36, latest_finish=90),
        TaskSpec("T4", "chiller", "制冷", [
            SegmentSpec(6, 250, SegmentKind.NORMAL),
        ], earliest_start=32, latest_finish=90, max_pauses=1),
    ]
    base_load = {t: 300.0 for t in range(96)}
    return CoordinationService.create(
        config=config,
        devices=devices,
        tasks=tasks,
        tariff=tariff,
        windows=windows,
        base_load=base_load,
        storage_path=storage_path,
    )


def notice(nid="N1", start=36, end=48, cap=800.0):
    return GridNotice(nid, start, end, cap)


class NoticeAndPlanVersionTest(unittest.TestCase):
    def test_notice_triggers_comparable_plan_and_retry_merges(self):
        svc = build_service()
        v1 = svc.get_plan("v1")
        self.assertEqual(v1.state, PlanState.CALCULATED)

        v2 = svc.ingest_grid_notice(notice())
        self.assertEqual(v2.trigger["type"], "grid_notice")
        self.assertEqual(v2.parent_id, "v1")

        # 电网指令上限被满足（W1 峰值 ≤ 800）
        self.assertLessEqual(v2.windows["W1"].peak_kw, 800.0)

        # 与上一版可比较：T1/T2 被移动
        moved = {m["task_id"] for m in v2.diff_summary["moved_tasks"]}
        self.assertIn("T1", moved)
        self.assertIn("T2", moved)

        # 峰值归因：峰值来自哪些任务清晰可查
        attr = svc.get_peak_attribution("W1", v2.id)
        sources = {c["source"] for c in attr["contributions"]}
        self.assertIn("base", sources)
        self.assertTrue(attr["peak_kw"] <= 800.0)

        # 削减代价：T1 为避峰晚完工，T2 被移峰
        sacrificed = {s.task_id for s in v2.sacrifices}
        self.assertIn("T1", sacrificed)
        self.assertIn("T2", sacrificed)
        # T1 最晚完工被突破 → 未满足约束如实记录
        unmet = {(u.constraint, u.task_id) for u in v2.unmet}
        self.assertIn(("latest_finish", "T1"), unmet)

        # 工艺约束仍然成立：T1 各段连续、与 T2 不重叠（互锁）
        t1 = v2.task_slots("T1")
        t2 = v2.task_slots("T2")
        for lo, hi in ((48, 52), (52, 58)):  # 升温段/保温段各连续
            seg_slots = sorted(
                s for s, r in t1.items() if r.source != "executed" and lo <= s < hi
            )
            self.assertEqual(seg_slots, list(range(lo, hi)))
        self.assertFalse(set(t1) & set(t2))

        # 相同通知重试 → 合并为一次决策，不产生新版本
        again = svc.ingest_grid_notice(notice())
        self.assertEqual(again.id, v2.id)
        self.assertEqual(len(svc.plans), 2)
        # 同 id 不同载荷 → 拒绝
        with self.assertRaises(ValueError):
            svc.ingest_grid_notice(notice(cap=700.0))

        # 版本对比接口
        cmp = svc.compare_plans("v2")
        self.assertEqual(cmp["compared_to"], "v1")
        self.assertTrue(cmp["sacrifices"])

    def test_approve_then_activate_on_clock(self):
        svc = build_service()
        v2 = svc.ingest_grid_notice(notice())
        svc.approve_plan(v2.id, by="energy_supervisor")
        self.assertEqual(svc.get_plan(v2.id).state, PlanState.APPROVED)
        svc.advance_clock(37)
        self.assertEqual(svc.get_plan(v2.id).state, PlanState.ACTIVE)
        # 已执行槽位出现在后续计划中且不可改写
        v_after = svc.apply_forecast_deviation(
            ForecastDeviation(kind="base_load", start_slot=60, end_slot=72, delta_kw=50)
        )
        t3 = v_after.task_slots("T3")
        self.assertEqual(t3[36].source, "executed")


class ArbitrationTest(unittest.TestCase):
    def _setup_conflict(self, until=76):
        """v3 先算出（移动了 T2），随后经理保护 T2，主管再批准 v3 → 冲突。"""
        svc = build_service()
        svc.ingest_grid_notice(notice())
        svc.approve_plan("v2")
        svc.advance_clock(40)
        v3 = svc.ingest_grid_notice(notice("N2", 56, 72, 600.0))
        self.assertEqual(v3.state, PlanState.CALCULATED)
        prot = svc.create_protection("T2", until_slot=until, reason="订单赶工")
        svc.approve_plan(v3.id)
        v3 = svc.get_plan(v3.id)
        self.assertEqual(v3.state, PlanState.AWAITING_ARBITRATION)
        self.assertEqual(len(svc.cases), 1)
        case = next(iter(svc.cases.values()))
        self.assertEqual(case.protection_id, prot.id)
        return svc, v3, prot, case

    def test_conflict_goes_to_arbitration_then_energy_priority(self):
        svc, v3, prot, case = self._setup_conflict()
        plan = svc.arbitrate(case.id, ArbitrationResult.ENERGY_PRIORITY, by="arbiter")
        self.assertEqual(plan.id, v3.id)
        self.assertEqual(plan.state, PlanState.APPROVED)
        prot = svc.protections[prot.id]
        self.assertEqual(prot.status, ProtectionStatus.OVERRIDDEN)
        self.assertEqual(prot.arbitration_result, "energy_priority")
        decisions = svc.get_protection_decisions()
        self.assertEqual(decisions[0]["case"]["status"], "resolved")

    def test_arbitration_production_protected(self):
        svc, v3, prot, case = self._setup_conflict()
        v4 = svc.arbitrate(case.id, ArbitrationResult.PRODUCTION_PROTECTED)
        self.assertEqual(v4.state, PlanState.APPROVED)
        self.assertEqual(svc.get_plan(v3.id).state, PlanState.CLOSED)
        # T2 按保护基线锁定（v2 中的 60-68）
        t2 = v4.task_slots("T2")
        locked = sorted(s for s, r in t2.items() if r.source == "protection_lock")
        self.assertEqual(locked, list(range(60, 68)))
        # 锁定导致指令上限无法满足 → 如实记录
        self.assertIn("demand_cap", {u.constraint for u in v4.unmet})
        prot = svc.protections[prot.id]
        self.assertEqual(prot.status, ProtectionStatus.UPHELD)
        self.assertEqual(prot.scope, "full")

    def test_arbitration_time_limited_exception(self):
        svc, v3, prot, case = self._setup_conflict(until=64)
        v4 = svc.arbitrate(case.id, ArbitrationResult.TIME_LIMITED_EXCEPTION)
        self.assertEqual(v4.state, PlanState.APPROVED)
        t2 = v4.task_slots("T2")
        # 期限内（60-64）锁定，期限外由能源侧调度
        locked = sorted(s for s, r in t2.items() if r.source == "protection_lock")
        self.assertEqual(locked, list(range(60, 64)))
        free = sorted(s for s, r in t2.items() if r.source == "planned")
        self.assertTrue(all(s >= 64 for s in free))
        prot = svc.protections[prot.id]
        self.assertEqual(prot.scope, "window")

    def test_protection_conflicting_with_active_plan_opens_case(self):
        svc = build_service()
        svc.ingest_grid_notice(notice())
        svc.approve_plan("v2")
        svc.advance_clock(40)
        v2 = svc.get_plan("v2")
        self.assertEqual(v2.state, PlanState.ACTIVE)
        # 经理要求 T2 改从 56 开始运行 → 与生效计划（60-68）冲突
        svc.create_protection(
            "T2", until_slot=70, reason="现场要求提前", expected_start_slot=56
        )
        self.assertEqual(svc.get_plan("v2").state, PlanState.AWAITING_ARBITRATION)
        self.assertEqual(len(svc.cases), 1)


class ClockAndReportTest(unittest.TestCase):
    def test_clock_across_windows_reports_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            svc = build_service(storage_path=path)
            svc.ingest_grid_notice(notice())
            svc.approve_plan("v2")
            # 跨过 W1：基荷实测高于预测
            svc.advance_clock(
                48,
                actual_overrides={t: {"base": 400.0} for t in range(44, 48)},
            )
            w1 = svc.get_window_report("W1")
            self.assertEqual(w1["elapsed_slots"], 16)
            self.assertEqual(w1["planned"]["peak_kw_elapsed"], 500.0)
            self.assertEqual(w1["actual"]["peak_kw"], 550.0)  # 400 基荷 + 150 暖机
            # 费用差额 = 4 槽位 × 100kW × 0.25h × 1.2 元
            self.assertAlmostEqual(w1["cost_diff_elapsed"], 120.0, places=3)

            # T4 在 48 未按计划执行 → 执行偏差被记录
            svc.advance_clock(54, actual_overrides={48: {"tasks": {"T4": 0.0}}})
            incidents = {i.constraint for i in svc.incidents}
            self.assertIn("planned_not_executed", incidents)

            # 保温阶段锁定：53 起 T1 进入保温，剩余安排不可改写
            self.assertIn("T1", svc.holding_locks)
            v_dev = svc.apply_forecast_deviation(
                ForecastDeviation(kind="base_load", start_slot=60, end_slot=72,
                                  delta_kw=150.0)
            )
            self.assertEqual(v_dev.trigger["type"], "forecast_deviation")
            locked_t1 = {
                s: r.kw for s, r in v_dev.task_slots("T1").items() if s >= 54
            }
            self.assertEqual(
                locked_t1,
                {s: rec["kw"] for s, rec in svc.holding_locks["T1"].items() if s >= 54},
            )
            # 已执行槽位原样保留在新版本中
            for tid, slots in svc.executed.items():
                for s, rec in slots.items():
                    self.assertEqual(
                        v_dev.assignments[tid][s].kw, rec["kw"], f"{tid}@{s}"
                    )
                    self.assertEqual(v_dev.assignments[tid][s].source, "executed")

            # 现场强制运行 T2
            v_f = svc.apply_forced_run(ForcedRun("T2", 64, "现场要求"))
            t2 = v_f.task_slots("T2")
            forced = sorted(s for s, r in t2.items() if r.source == "forced")
            self.assertEqual(forced, list(range(64, 72)))

            # 保护与批准冲突 → 仲裁（能源优先）→ 生效
            svc.create_protection("T2", until_slot=70, reason="保供")
            svc.approve_plan(v_f.id)
            self.assertEqual(
                svc.get_plan(v_f.id).state, PlanState.AWAITING_ARBITRATION
            )
            case = next(iter(svc.cases.values()))
            svc.arbitrate(case.id, ArbitrationResult.ENERGY_PRIORITY)
            self.assertEqual(svc.get_plan(v_f.id).state, PlanState.APPROVED)

            # 连续推进跨过 W2、W3
            svc.advance_clock(96)
            self.assertEqual(svc.now_slot, 96)
            w2 = svc.get_window_report("W2")
            self.assertEqual(w2["elapsed_slots"], 24)
            self.assertIsNotNone(w2["actual"]["peak_kw"])
            unmet_text = json_dump = str(w2["unmet_constraints"])
            self.assertIn("latest_finish", unmet_text)          # T1 晚完工
            self.assertIn("planned_not_executed", unmet_text)   # T4 偏差
            w3 = svc.get_window_report("W3")
            self.assertEqual(w3["elapsed_slots"], 24)

            # 保护决定可查：已被仲裁覆盖
            decisions = svc.get_protection_decisions()
            self.assertEqual(decisions[0]["status"], "overridden")
            self.assertEqual(decisions[0]["arbitration_result"], "energy_priority")

            # 任务视图
            t1_report = svc.get_task_report("T1")
            self.assertEqual(t1_report["state"], "completed")
            t2_report = svc.get_task_report("T2")
            self.assertEqual(t2_report["planned_pauses"], 0)

            # ---- 重启：已发生的用能不会被重新调度 ----
            executed_before = {
                tid: dict(slots) for tid, slots in svc.executed.items()
            }
            svc2 = CoordinationService.load(path)
            self.assertEqual(svc2.now_slot, 96)
            self.assertEqual(svc2.executed, executed_before)
            w1_after = svc2.get_window_report("W1")
            self.assertEqual(w1_after["actual"]["peak_kw"], 550.0)

    def test_restart_does_not_reschedule_past(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            svc = build_service(storage_path=path)
            svc.ingest_grid_notice(notice())
            svc.approve_plan("v2")
            svc.advance_clock(
                54, actual_overrides={t: {"base": 400.0} for t in range(44, 48)}
            )
            svc2 = CoordinationService.load(path)
            v_new = svc2.ingest_grid_notice(notice("N9", 60, 72, 900.0))
            # 重启后的新计划不触碰任何已执行槽位
            for tid, slots in svc2.executed.items():
                for s, rec in slots.items():
                    slot = v_new.assignments[tid][s]
                    self.assertEqual(slot.kw, rec["kw"])
                    self.assertEqual(slot.source, "executed")
            # 实际需量查询不受重启影响
            self.assertEqual(
                svc2.get_window_report("W1")["actual"]["peak_kw"], 550.0
            )


class ProtectionExpiryTest(unittest.TestCase):
    def test_protection_expires_with_clock(self):
        svc = build_service()
        svc.ingest_grid_notice(notice())
        svc.approve_plan("v2")
        svc.advance_clock(40)
        prot = svc.create_protection("T2", until_slot=50, reason="短保")
        svc.advance_clock(52)
        self.assertEqual(
            svc.protections[prot.id].status, ProtectionStatus.EXPIRED
        )
        # 过期保护不再锁定任务
        locked = svc._build_locked()
        self.assertFalse(
            any(r.source == "protection_lock" for r in locked.get("T2", {}).values())
        )


if __name__ == "__main__":
    unittest.main()
