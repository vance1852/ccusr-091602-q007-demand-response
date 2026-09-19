"""端到端演示：电网临时需量控制通知下的滚动协同。

运行：python3 examples/demo_scenario.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_service import build_service, notice  # noqa: E402
from app import (  # noqa: E402
    ArbitrationResult,
    ForecastDeviation,
    ForcedRun,
    GridNotice,
)


def show_plan(svc, plan):
    print(f"\n== 计划 {plan.id}（{plan.state.value}）触发: {plan.trigger['type']} ==")
    print(f"   总费用 {plan.total_cost:,.1f} 元（电费 {plan.total_energy_cost:,.1f} + "
          f"需量罚金 {plan.total_demand_penalty:,.1f}）")
    for m in plan.diff_summary.get("moved_tasks", []):
        print(f"   移动: {m['task_id']}  {m['old_start']}→{m['new_start']}")
    for s in plan.sacrifices:
        print(f"   牺牲: {s.detail}（电费变化 {s.cost_delta:+.1f} 元）")
    for u in plan.unmet:
        print(f"   未满足: {u.detail}")


def main():
    svc = build_service()
    print("初始计划 v1（按原计划生产）")
    show_plan(svc, svc.get_plan("v1"))

    print("\n--- 09:00 电网下发需量控制通知：槽位 36-48 上限 800kW ---")
    v2 = svc.ingest_grid_notice(notice())
    show_plan(svc, v2)
    attr = svc.get_peak_attribution("W1", v2.id)
    print(f"   W1 峰值 {attr['peak_kw']:.0f}kW @槽位{attr['peak_slot']}，构成: "
          + ", ".join(f"{c['source']}={c['kw']:.0f}" for c in attr["contributions"]))

    print("\n--- 同一通知重试（合并为一次决策）---")
    again = svc.ingest_grid_notice(notice())
    print(f"   返回计划 {again.id}，版本数仍为 {len(svc.plans)}")

    svc.approve_plan(v2.id, by="energy_supervisor")
    svc.advance_clock(40)
    print(f"\n--- 时钟推进到槽位 40（{svc.config.slot_to_iso(40)}），v2 生效执行 ---")

    print("\n--- 第二张通知：槽位 56-72 上限 600kW ---")
    v3 = svc.ingest_grid_notice(GridNotice("N2", 56, 72, 600.0))
    show_plan(svc, v3)

    print("\n--- 生产经理对 T2 提出期限保护（至槽位 76）---")
    svc.create_protection("T2", until_slot=76, reason="订单赶工")
    svc.approve_plan(v3.id, by="energy_supervisor")
    print(f"   批准与保护冲突 → v3 状态: {svc.get_plan('v3').state.value}")
    case = next(iter(svc.cases.values()))
    print(f"   待仲裁事项 {case.id}: {case.reason}")

    print("\n--- 仲裁：生产受保护，重算 ---")
    v4 = svc.arbitrate(case.id, ArbitrationResult.PRODUCTION_PROTECTED)
    show_plan(svc, v4)

    print("\n--- 基荷预测偏差 +150kW（槽位 60-72），滚动重算 ---")
    v5 = svc.apply_forecast_deviation(
        ForecastDeviation(kind="base_load", start_slot=60, end_slot=72, delta_kw=150)
    )
    show_plan(svc, v5)
    svc.approve_plan(v5.id, by="energy_supervisor")

    print("\n--- 现场强制运行 T4 自槽位 74 ---")
    v6 = svc.apply_forced_run(ForcedRun("T4", 74, "演示"))
    show_plan(svc, v6)
    svc.approve_plan(v6.id, by="energy_supervisor")

    print("\n--- 时钟连续推进跨过三个计费窗口 ---")
    svc.advance_clock(96, actual_overrides={t: {"base": 380.0} for t in range(60, 64)})
    for wid in ("W1", "W2", "W3"):
        r = svc.get_window_report(wid)
        print(
            f"   {wid} [{r['start_iso'][11:16]}→{r['end_iso'][11:16]}] "
            f"计划峰值 {r['planned']['peak_kw_elapsed']}kW / "
            f"实际峰值 {r['actual']['peak_kw']}kW，"
            f"费用差额 {r['cost_diff_elapsed']:+.1f} 元，"
            f"未满足约束 {len(r['unmet_constraints'])} 项"
        )
    print("\n保护决定:")
    for d in svc.get_protection_decisions():
        print(f"   {d['id']} 任务{d['task_id']} 至槽位{d['until_slot']}: "
              f"{d['status']}（仲裁: {d['arbitration_result']}）")


if __name__ == "__main__":
    main()
