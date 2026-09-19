# 制造用能削峰协同

本项目把生产任务的功率需求与园区需量约束放在同一时间轴中。工艺不可中断区间、设备爬坡和电网通知均作为版本化输入，已发生的用能数据保持不可变。

`domain_contract.json` 约定计划状态、负荷类型和仲裁结论。功率单位为 kW，能量单位为 kWh，计费窗口带明确时区与起止边界。

## 架构

- `app/models.py` — 领域模型：设备（互锁组/每日启停上限/恢复爬坡）、任务功率曲线
  （普通/不可中断/保温段）、分时电价、需量窗口、电网通知、保护、计划版本。
  时间轴为等长槽位（slot），槽位 0 对应 `ServiceConfig.epoch_iso`（含时区）。
- `app/scheduler.py` — 滚动调度引擎：逐任务动态规划放置曲线段
  （不可中断/保温段整体连续，interruptible 任务可拆分到槽位粒度），
  硬约束（互锁、设备容量、恢复爬坡暖机、每日启停次数）在转移时检查，
  电网指令上限以高额罚分软约束；削峰修复循环重排可移峰任务；
  每个任务与"无指令、无他负荷"的理想基线对比，记录削减牺牲了什么。
- `app/service.py` — 协同服务：事件入口（电网通知幂等去重、现场强制运行、
  预测偏差）、审批流（能源主管批准 / 生产经理有期限保护 / 冲突待仲裁）、
  模拟时钟（执行结算、保温锁定、保护到期）、查询（窗口账单、峰值归因、
  计划对比、保护决定）与 JSON 持久化。

## 关键语义

- **不可改写**：已执行槽位（抄表实际值）与进入保温阶段的任务剩余安排，
  在任何后续版本中都原样保留；服务重启后亦不会重新调度已发生的用能。
- **幂等决策**：相同 id 且载荷一致的电网通知重试，返回首次决策的计划版本，
  不产生新版本；同 id 不同载荷会被拒绝。
- **待仲裁**：批准的计划若改写了生效保护的基线（或新保护与已批准/生效计划
  冲突），计划进入 `awaiting_arbitration`；仲裁结论为
  `energy_priority`（覆盖保护）/ `production_protected`（整段锁定重算）/
  `time_limited_exception`（仅期限内锁定）。
- **计划状态机**：`calculated → approved → active → closed`，冲突时
  `calculated/approved/active → awaiting_arbitration`。

## 运行

```bash
python3 -m unittest discover -s tests -v   # 测试
python3 examples/demo_scenario.py          # 端到端演示
```

## API 速览

```python
from app import CoordinationService, GridNotice, ForcedRun, ForecastDeviation, ArbitrationResult

svc = CoordinationService.create(config=..., devices=..., tasks=..., tariff=...,
                                 windows=..., base_load=..., storage_path="state.json")
v2 = svc.ingest_grid_notice(GridNotice("N1", 36, 48, 800.0))   # 重试同通知 → 返回 v2
svc.approve_plan(v2.id, by="energy_supervisor")                # 冲突 → awaiting_arbitration
prot = svc.create_protection("T2", until_slot=76, reason="赶工")
svc.arbitrate(case_id, ArbitrationResult.PRODUCTION_PROTECTED) # 三种结论之一
svc.advance_clock(48, actual_overrides={44: {"base": 400.0}})  # 推进模拟时钟
svc.get_window_report("W1")        # 实际 vs 计划需量、费用差额、未满足约束
svc.get_peak_attribution("W1")     # 峰值来自哪些任务
svc.compare_plans("v2")            # 与上一版对比
svc.get_protection_decisions()     # 保护决定与仲裁结论
svc2 = CoordinationService.load("state.json")                  # 重启不扰动历史
```
