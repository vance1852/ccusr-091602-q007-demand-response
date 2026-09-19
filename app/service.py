"""制造负荷与生产承诺协同服务（应用服务层）。

职责：
- 接收电网通知并合并重试，触发滚动重算，产出可与上一版比较的计划版本；
- 能源主管批准方案；生产经理对单任务提出有期限保护；冲突进入待仲裁；
- 推进模拟时钟时冻结实测（允许注入预测偏差），已发生用能不被重算改写；
- 提供跨计费窗口的实际/计划需量、费用差额、未满足约束与保护决定查询。
"""

from __future__ import annotations

from typing import Any

from . import timegrid as tg
from .models import GridDirective
from .repository import Protection, Warehouse
from .scheduler import Scheduler

VALID_RESOLUTIONS = ("energy_priority", "production_protected", "time_limited_exception")


class CoordinationService:
    def __init__(self, warehouse: Warehouse) -> None:
        self.wh = warehouse

    # ------------------------------------------------------------ 计划生成

    def recalculate(self, notice_key: str | None = None, reason: str = "manual") -> dict[str, Any]:
        """执行一次滚动重算并持久化新版本；返回计划 dict。"""
        merged = []
        if notice_key and notice_key in self.wh.directive_retry_seqs:
            merged = list(self.wh.directive_retry_seqs[notice_key])
        scheduler = Scheduler(self.wh)
        plan = scheduler.run(notice_key, merged, reason)
        # 旧版本收口为 closed（不可变历史保留）
        for old in self.wh.plans:
            if old["state"] in ("calculated", "awaiting_arbitration", "approved", "active"):
                old["state"] = "closed"
        self.wh.append_plan(plan.to_dict())
        return self.wh.latest_plan()

    def receive_grid_notice(self, directive: GridDirective) -> dict[str, Any]:
        effective, triggers = self.wh.receive_directive(directive)
        if not triggers:
            latest = self.wh.latest_plan()
            if latest is not None and latest.get("notice_key") == directive.notice_key:
                seqs = sorted(set(latest["merged_retry_seqs"]) | {directive.retry_seq})
                self.wh.update_plan_state(latest["version"], merged_retry_seqs=seqs)
                latest = self.wh.latest_plan()
            return {
                "merged": True,
                "notice_key": directive.notice_key,
                "retry_seq": directive.retry_seq,
                "plan_version": latest["version"] if latest else None,
                "message": "相同通知重试已合并，未产生新决策",
            }
        plan = self.recalculate(
            effective.notice_key,
            reason=f"电网通知 {effective.notice_key}："
                   f"{tg.iso(effective.start)}~{tg.iso(effective.end)} "
                   f"需量限值 {effective.demand_limit_kw:.0f} kW",
        )
        return {"merged": False, "notice_key": directive.notice_key, "plan_version": plan["version"]}

    def report_forecast_deviation(self, deviations: dict[str, float]) -> dict[str, Any]:
        """现场上报当前槽预测偏差（kW 修正），立即重算未来计划。"""
        now_iso = self.wh.clock.now_iso()
        latest = self.wh.latest_plan()
        if latest is None:
            raise RuntimeError("尚无基准计划")
        idx = tg.slot_index(latest["horizon_start"], now_iso)
        kw_by_task = {t["task_id"]: t["kw"][idx] for t in latest["tasks"]}
        for tid, delta in deviations.items():
            kw_by_task[tid] = max(0.0, kw_by_task.get(tid, 0.0) + delta)
        self.wh.record_actuals({now_iso: kw_by_task})
        return self.recalculate(reason=f"现场预测偏差修正：{deviations}")

    def force_run(self, task_id: str, start: str, end: str) -> dict[str, Any]:
        """现场强制运行：区间按额定功率锁定（工艺不可中断，可超出原运行范围）。"""
        task = self.wh.get_task(task_id)
        task.forced_segments.append((tg.iso(tg.floor_slot(start)), tg.iso(tg.floor_slot(end))))
        self.wh.upsert_task(task)
        return self.recalculate(reason=f"现场强制运行：{task.name} {start}~{end}")

    # ------------------------------------------------------------ 审批 / 保护 / 仲裁

    def approve(self, by: str = "energy_manager", version: int | None = None) -> dict[str, Any]:
        plan = self.wh.plan_by_version(version) if version else self.wh.latest_plan()
        if plan is None:
            raise RuntimeError("没有可批准的计划")
        if plan["state"] == "awaiting_arbitration":
            raise PermissionError("计划存在待仲裁的保护冲突，仲裁后才能批准")
        hard = [v for v in plan["violations"] if v.get("severity", "hard") == "hard"]
        if hard:
            raise PermissionError(
                "计划仍有未满足的硬约束（" +
                "；".join(v["code"] for v in hard) + "），不可批准")
        self.wh.update_plan_state(
            plan["version"], state="approved", approved_by=by,
            approved_at=self.wh.clock.now_iso(),
        )
        return self.wh.plan_by_version(plan["version"])

    def protect_task(self, task_id: str, minutes: int, reason: str,
                     by: str = "production_manager") -> dict[str, Any]:
        """生产经理对单个任务提出自现在起 minutes 分钟的有期限保护。"""
        task = self.wh.get_task(task_id)
        start = self.wh.clock.now_iso()
        end = tg.iso(tg.slot_at(tg.floor_slot(start), minutes // tg.SLOT_MINUTES))
        protection = Protection(
            protection_id=f"prot_{len(self.wh.protections) + 1:03d}",
            task_id=task_id, start=start, end=end, reason=reason, created_by=by,
        )
        self.wh.add_protection(protection)
        latest = self.wh.latest_plan()
        notice = latest["notice_key"] if latest else None
        plan = self.recalculate(notice, reason=f"生产保护：{task.name} 至 {end}（{reason}）")
        return {
            "protection_id": protection.protection_id,
            "plan_version": plan["version"],
            "state": plan["state"],
            "conflicts": plan["protection_conflicts"],
        }

    def arbitrate(self, protection_id: str, resolution: str, by: str = "energy_manager") -> dict[str, Any]:
        if resolution not in VALID_RESOLUTIONS:
            raise ValueError(f"仲裁结论必须是 {VALID_RESOLUTIONS} 之一")
        protection = self.wh.resolve_protection(protection_id, resolution, by)
        latest = self.wh.latest_plan()
        notice = latest["notice_key"] if latest else None
        plan = self.recalculate(
            notice,
            reason=f"仲裁 {protection_id}：{resolution}（{by}）",
        )
        # 把仲裁结论回填到本版本的冲突记录上并持久化
        for conflict in plan["protection_conflicts"]:
            if conflict["protection_id"] == protection_id and conflict["resolution"] is None:
                conflict["resolution"] = resolution
                conflict["resolved_by"] = by
                conflict["resolved_at"] = self.wh.clock.now_iso()
        if plan["state"] == "awaiting_arbitration" and all(
            c["resolution"] is not None for c in plan["protection_conflicts"]
        ):
            self.wh.update_plan_state(plan["version"], state="calculated",
                                     protection_conflicts=plan["protection_conflicts"])
        else:
            self.wh.update_plan_state(plan["version"],
                                     protection_conflicts=plan["protection_conflicts"])
        plan = self.wh.plan_by_version(plan["version"])
        return {"protection": protection.to_dict(), "plan_version": plan["version"],
                "state": plan["state"]}

    # ------------------------------------------------------------ 时钟推进 / 实测

    def advance(self, minutes: int, meter_overrides: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
        """推进模拟时钟并冻结跨越槽位的实测。

        meter_overrides: {相对偏移分钟(含) : {task_id: kw}} 用于注入预测/实测偏差；
        缺省按当前执行版本（已批准优先，否则最新版）落地功率。
        """
        if minutes % tg.SLOT_MINUTES != 0:
            raise ValueError("推进时长须对齐槽位")
        before_plan = self.wh.latest_plan()
        steps = minutes // tg.SLOT_MINUTES
        h_start = before_plan["horizon_start"]
        width = len(before_plan["tasks"][0]["kw"]) if before_plan["tasks"] else 0
        frozen = {}
        for step in range(1, steps + 1):
            slot_moment = self.wh.clock.now()
            idx = tg.slot_index(h_start, slot_moment)
            if 0 <= idx < width:
                kw_by_task = {t["task_id"]: t["kw"][idx] for t in before_plan["tasks"]}
            else:
                kw_by_task = {}
            override = (meter_overrides or {}).get(step * tg.SLOT_MINUTES)
            if override:
                kw_by_task.update(override)
            frozen[tg.iso(slot_moment)] = {k: v for k, v in kw_by_task.items()}
            self.wh.advance_clock(tg.SLOT_MINUTES)
        if frozen:
            self.wh.record_actuals(frozen)
            if before_plan["state"] == "approved":
                self.wh.update_plan_state(before_plan["version"], state="active")
        # 关闭跨过的计费窗口：快照执行版本的计划值，供日后实际 vs 计划对照
        executed = before_plan
        now_iso = self.wh.clock.now_iso()
        for w in self.wh.windows:
            if w.end <= now_iso and self.wh.window_snapshot(w.window_id) is None:
                stat = next((x for x in executed["windows"]
                             if x["window_id"] == w.window_id), None)
                if stat is not None:
                    self.wh.snapshot_window(w.window_id, {
                        "version": executed["version"],
                        "planned_demand_kw": stat["planned_demand_kw"],
                        "planned_energy_cost": stat["planned_energy_cost"],
                        "demand_charge": stat["demand_charge"],
                        "penalty": stat["penalty"],
                    })
        # 滚动重算：时间推进本身产生新版本（未来视野改变），通知重试不会
        notice = executed["notice_key"]
        plan = self.recalculate(notice, reason=f"时钟推进 {minutes} 分钟，滚动重算")
        return {"frozen_slots": len(frozen), "plan_version": plan["version"]}

    # ------------------------------------------------------------ 查询

    def latest_plan(self) -> dict[str, Any] | None:
        return self.wh.latest_plan()

    def plan(self, version: int) -> dict[str, Any] | None:
        return self.wh.plan_by_version(version)

    def compare(self, version_a: int, version_b: int) -> dict[str, Any]:
        a, b = self.wh.plan_by_version(version_a), self.wh.plan_by_version(version_b)
        if a is None or b is None:
            raise KeyError("计划版本不存在")
        changed = []
        va = {t["task_id"]: t for t in a["tasks"]}
        for tb in b["tasks"]:
            old = va.get(tb["task_id"])
            if old is None:
                changed.append({"task_id": tb["task_id"], "change": "new"})
                continue
            slots = [i for i in range(len(tb["kw"]))
                     if abs(tb["kw"][i] - old["kw"][i]) > 1e-6]
            if slots or old["status"] != tb["status"]:
                changed.append({
                    "task_id": tb["task_id"],
                    "name": self.wh.get_task(tb["task_id"]).name,
                    "status_from": old["status"],
                    "status_to": tb["status"],
                    "slots_changed": len(slots),
                    "first_changed_slot": tg.iso(tg.slot_at(b["horizon_start"], slots[0]))
                    if slots else None,
                })
        return {
            "version_from": version_a,
            "version_to": version_b,
            "peak_from_kw": max(a["total_kw"]),
            "peak_to_kw": max(b["total_kw"]),
            "changed_tasks": changed,
            "sacrifices_to": b["sacrifices"],
            "violations_to": b["violations"],
        }

    def dashboard(self) -> dict[str, Any]:
        plan = self.wh.latest_plan()
        windows = []
        total_actual_cost = total_planned_cost = total_baseline_cost = 0.0
        total_penalty = 0.0
        for w in (plan["windows"] if plan else []):
            planned = w["planned_energy_cost"] + w["demand_charge"] + w["penalty"]
            baseline = w["baseline_total_cost"]
            actual = (w["actual_energy_cost"] + (w["actual_demand_charge"] or 0)
                      + (w["actual_penalty"] or 0)) \
                if w["actual_energy_cost"] is not None else None
            if actual is not None:
                total_actual_cost += actual
            total_planned_cost += planned
            total_baseline_cost += baseline
            total_penalty += w["penalty"]
            windows.append({
                "window_id": w["window_id"],
                "start": w["start"], "end": w["end"],
                "baseline_demand_kw": w["baseline_demand_kw"],
                "planned_demand_kw": w["planned_demand_kw"],
                "actual_demand_kw": w["actual_demand_kw"],
                "demand_delta_kw": (
                    round((w["actual_demand_kw"] or 0) - w["planned_demand_kw"], 3)
                    if w["actual_demand_kw"] is not None else None),
                "contracted_kw": w["contracted_kw"],
                "planned_penalty": w["penalty"],
                "actual_penalty": w["actual_penalty"],
                "planned_total_cost": round(planned, 2),
                "baseline_total_cost": round(baseline, 2),
                "actual_total_cost": round(actual, 2) if actual is not None else None,
                "actual_vs_planned_cost": round(actual - planned, 2) if actual is not None else None,
                "executed_version": w.get("executed_version"),
            })
        protections = []
        for p in self.wh.protections:
            protections.append({
                "protection_id": p.protection_id,
                "task_id": p.task_id,
                "task_name": self.wh.get_task(p.task_id).name,
                "start": p.start, "end": p.end, "reason": p.reason,
                "active": p.active_at(self.wh.clock.now()),
                "resolution": p.resolution,
                "resolved_by": p.resolved_by,
            })
        return {
            "now": self.wh.clock.now_iso(),
            "latest_version": plan["version"] if plan else None,
            "plan_state": plan["state"] if plan else None,
            "approved": bool(plan and plan.get("approved_by")),
            "windows": windows,
            "cost": {
                "baseline_closed_or_all": round(total_baseline_cost, 2),
                "planned_all_windows": round(total_planned_cost, 2),
                "actual_closed_windows": round(total_actual_cost, 2),
                "planned_penalty_all": round(total_penalty, 2),
                "saved_vs_baseline_all": round(total_baseline_cost - total_planned_cost, 2),
            },
            "unmet_constraints": plan["violations"] if plan else [],
            "pending_arbitration": [
                c for c in (plan["protection_conflicts"] if plan else [])
                if c["resolution"] is None
            ],
            "protections": protections,
            "peaks": plan["peaks"] if plan else [],
            "sacrifices": plan["sacrifices"] if plan else [],
        }
