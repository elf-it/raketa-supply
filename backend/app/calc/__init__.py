"""Расчётное ядро.

Чистый Python без SQLAlchemy и HTTP — см. `docs/01-architecture.md`.
"""

from .aggregate import aggregate, aggregate_cells, worst_status
from .purchase import apply_constraints, projected_stock, safety_stock, suggest_purchase
from .series import compute_series, original_forecast, seasonal_demand
from .types import (
    CalcSettings,
    Level,
    PurchaseParams,
    PurchaseSuggestion,
    SeriesInput,
    SeriesResult,
    Status,
    WeekCell,
)
from .weeks import (
    shift_week,
    week_end,
    week_key,
    week_number,
    week_range,
    week_start,
    weeks_between,
)

__all__ = [
    "CalcSettings",
    "Level",
    "PurchaseParams",
    "PurchaseSuggestion",
    "SeriesInput",
    "SeriesResult",
    "Status",
    "WeekCell",
    "aggregate",
    "aggregate_cells",
    "apply_constraints",
    "compute_series",
    "original_forecast",
    "projected_stock",
    "safety_stock",
    "seasonal_demand",
    "shift_week",
    "suggest_purchase",
    "week_end",
    "week_key",
    "week_number",
    "week_range",
    "week_start",
    "weeks_between",
    "worst_status",
]
