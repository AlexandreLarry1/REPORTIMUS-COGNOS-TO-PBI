"""Canonical Cognos-aggregate -> DAX / PBI mapping.

Single source of truth for aggregation semantics. Cognos `dataItem` elements
carry an `aggregate` attribute (e.g. "maximum", "minimum", "average") that
must be respected when generating DAX measures, report.json Aggregation
wells, and model.bim column summarizeBy — instead of defaulting everything
to Sum. No other module should hardcode "SUM"/"Function: 0"/"summarizeBy":
"sum" for a value that has a known Cognos aggregate; route it through here.
"""

from __future__ import annotations

# Cognos dataItem @aggregate values (lowercased) -> DAX aggregation function name.
AGGREGATE_TO_DAX_FUNC: dict[str, str] = {
    "sum": "SUM",
    "total": "SUM",
    "average": "AVERAGE",
    "avg": "AVERAGE",
    "maximum": "MAX",
    "max": "MAX",
    "minimum": "MIN",
    "min": "MIN",
    "count": "COUNT",
    "count-distinct": "DISTINCTCOUNT",
    "distinct-count": "DISTINCTCOUNT",
    "countdistinct": "DISTINCTCOUNT",
}

# PBI report.json well "Aggregation.Function" integer codes.
# Empirically verified against a real PBI Desktop export (2026-07-02):
# 0=Sum, 1=Average, 2=DistinctCount, 3=Min, 4=Max, 5=Count.
AGGREGATE_TO_PBI_FUNCTION: dict[str, int] = {
    "sum": 0,
    "total": 0,
    "average": 1,
    "avg": 1,
    "count-distinct": 2,
    "distinct-count": 2,
    "countdistinct": 2,
    "minimum": 3,
    "min": 3,
    "maximum": 4,
    "max": 4,
    "count": 5,
}

# PBI model.bim column-level "summarizeBy" string tokens.
AGGREGATE_TO_SUMMARIZE_BY: dict[str, str] = {
    "sum": "sum",
    "total": "sum",
    "average": "average",
    "avg": "average",
    "maximum": "max",
    "max": "max",
    "minimum": "min",
    "min": "min",
    "count": "count",
    "count-distinct": "distinctCount",
    "distinct-count": "distinctCount",
    "countdistinct": "distinctCount",
}


def normalize(aggregate: str | None) -> str:
    return (aggregate or "none").strip().lower()


def dax_func(aggregate: str | None, default: str = "SUM") -> str:
    """DAX function name for a Cognos aggregate, or `default` if none/unknown."""
    return AGGREGATE_TO_DAX_FUNC.get(normalize(aggregate), default)


def pbi_function(aggregate: str | None, default: int = 0) -> int:
    """PBI report.json Aggregation.Function code, defaulting to Sum(0)."""
    return AGGREGATE_TO_PBI_FUNCTION.get(normalize(aggregate), default)


def summarize_by(aggregate: str | None, default: str = "none") -> str:
    """PBI model.bim column summarizeBy token."""
    return AGGREGATE_TO_SUMMARIZE_BY.get(normalize(aggregate), default)
