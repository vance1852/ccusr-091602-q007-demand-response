"""制造负荷与生产承诺协同服务 —— 验收演示。

连续推进模拟时钟跨过多个需量计费窗口，演示：
电网通知（重试合并）-> 滚动削峰计划（峰值归因/牺牲说明）-> 生产保护 ->
待仲裁 -> 仲裁与批准 -> 预测偏差与现场强制运行 -> 跨窗口实际/计划查询 ->
重启后已发生用能不被重算。

运行：python3 demo.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.repository import Warehouse
from app.scenario import build_directive, build_service
from app.service import CoordinationService


def show(plan, idx_from=0, idx_to=None):
    idx_to = idx_to or len(plan["total_kw"])
    print(f"  {'时刻':<8}{'总功率kW':>9}  {'限值':>6}")
    for i in range(idx_from, idx_to):
        moment = plan["horizon_start"][11:16]
        h = (8 + (i * 15) // 60)
        m = (i * 15) % 60
        limit = plan["directive_limit_kw"] if 4 <= i < 10 else ""
        print(f"  {h:02d}:{m:02d}   {plan['total_kw'][i]:>9.1f}  {limit!s:>6}")


def main() -> None:
    store = Path(tempfile.gettempdir()) / "factory_dr_demo.json"
    store.unlink(missing_ok=True)
    svc = build_service(store)

    print("① 初始基线（无通知）")
    v1 = svc.recalculate(reason="初始基线")
    print(f"   全视野峰值 {max(v1['total_kw']):.0f} kW，峰值归因：")
    for a in v1["peaks"][1]["attribution"][:3]:
        print(f"     - {a['name']} {a['kw']:.0f} kW")
    w02 = next(w for w in v1["windows"] if w["window_id"] == "W02")
    print(f"   W02 基线需量 {w02['baseline_demand_kw']:.0f} > 契约 "
          f"{w02['contracted_kw']:.0f}，超约罚金 {w02['penalty']:.0f} 元")

    print("\n② 08:50 收到电网需量控制通知 09:00-10:30 ≤ 700 kW（含 3 次相同重试）")
    print("  ", svc.receive_grid_notice(build_directive("2026-09-19T08:00", 0)))
    print("  ", svc.receive_grid_notice(build_directive("2026-09-19T08:00", 5)))
    print("  ", svc.receive_grid_notice(build_directive("2026-09-19T08:00", 9)))
    v2 = svc.latest_plan()
    print(f"   合并为一次决策（版本 {v2['version']}，重试 seq {v2['merged_retry_seqs']}）")
    print("   牺牲了什么：")
    for s in v2["sacrifices"]:
        print(f"     - {s['task_id']} {s['action']}：{s['detail']}，推迟完工 "
              f"{s['delay_minutes']} 分钟")
    show(v2, 4, 10)

    print("\n③ 生产经理对循环风机提出 150 分钟保护")
    r = svc.protect_task("fan_ramp", 150, "保温工件需要循环风量")
    print(f"   计划状态：{r['state']}（保护与削峰可共存，调度改用移峰压铸线）")

    print("\n④ 生产经理连续保护空压机1与压铸线 -> 冲突进入待仲裁")
    svc.protect_task("compressor_1", 150, "气源不可断")
    r = svc.protect_task("die_cast", 150, "压铸批次不可移")
    print(f"   计划状态：{r['state']}")
    for c in svc.latest_plan()["protection_conflicts"]:
        print(f"     - 待仲裁 {c['task_id']}（{c['protection_id']}）：{c['note'][:60]}…")

    print("\n⑤ 厂长仲裁：能源优先；能源主管批准")
    for c in svc.latest_plan()["protection_conflicts"]:
        svc.arbitrate(c["protection_id"], "energy_priority", by="plant_director")
    approved = svc.approve()
    print(f"   计划 {approved['version']} 状态 {approved['state']}，"
          f"批准人 {approved['approved_by']}")

    print("\n⑥ 连续推进模拟时钟 08:00 -> 12:30（9 个计费窗口），09:15 现场偏差 +30kW")
    svc.advance(270, meter_overrides={75: {"compressor_1": 30}})
    dash = svc.dashboard()
    print(f"   当前时钟 {dash['now']}")
    print(f"   {'窗口':<5}{'基线':>7}{'计划':>7}{'实际':>7}{'偏差':>7}{'实际罚金':>9}")
    for w in dash["windows"]:
        print(f"   {w['window_id']:<5}{w['baseline_demand_kw']:>7.0f}"
              f"{w['planned_demand_kw']:>7.0f}{(w['actual_demand_kw'] or 0):>7.0f}"
              f"{(w['demand_delta_kw'] or 0):>7.0f}{(w['actual_penalty'] or 0):>9.0f}")
    print("   费用：", json.dumps(dash["cost"], ensure_ascii=False))
    print("   未满足约束：", [v["code"] for v in dash["unmet_constraints"]] or "无")
    print("   保护决定：")
    for p in dash["protections"]:
        print(f"     - {p['task_name']} {p['start'][11:16]}~{p['end'][11:16]} "
              f"仲裁={p['resolution']}")

    print("\n⑦ 服务重启：重新加载仓库，已发生用能保持不可变")
    actuals_before = {a: r.kw_by_task for a, r in Warehouse(store).actuals.items()}
    svc2 = CoordinationService(Warehouse(store))
    svc2.recalculate(reason="重启后滚动")
    actuals_after = {a: r.kw_by_task for a, r in Warehouse(store).actuals.items()}
    print(f"   重启后时钟 {Warehouse(store).clock.now_iso()}，"
          f"{len(actuals_after)} 个历史槽位与重启前一致：{actuals_before == actuals_after}")
    print("\n演示完成。仓库文件：", store)


if __name__ == "__main__":
    main()
