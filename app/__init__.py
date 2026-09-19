"""制造用能削峰协同领域包。"""

from .models import (
    ArbitrationResult,
    DemandWindow,
    Device,
    ForecastDeviation,
    ForcedRun,
    GridNotice,
    LoadKind,
    PlanState,
    Protection,
    ProtectionStatus,
    SegmentKind,
    SegmentSpec,
    ServiceConfig,
    Tariff,
    TariffPeriod,
    TaskSpec,
)
from .service import CoordinationService

PROJECT_NAME = "factory-demand-response"

__all__ = [
    "PROJECT_NAME",
    "ArbitrationResult",
    "CoordinationService",
    "DemandWindow",
    "Device",
    "ForecastDeviation",
    "ForcedRun",
    "GridNotice",
    "LoadKind",
    "PlanState",
    "Protection",
    "ProtectionStatus",
    "SegmentKind",
    "SegmentSpec",
    "ServiceConfig",
    "Tariff",
    "TariffPeriod",
    "TaskSpec",
]
