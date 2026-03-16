from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TypedDict

#: Format used by the Enel API for date parameters (e.g. "01012024").
DATE_FMT = "%d%m%Y"

@dataclass(frozen=True)
class HourlyPoint:
    """A single hourly consumption reading."""

    timestamp: datetime
    kwh: float
    cumulative_kwh: float


class MonthBatch(TypedDict):
    """One month's worth of parsed data keyed by calendar date."""

    month: str                               # "YYYY-MM" label used for logging
    data_points: dict[date, list[HourlyPoint]]


def month_last_day(dt: datetime) -> datetime:
    """Return the last day of *dt*'s month, capped at today for the current month."""
    today = datetime.now()
    if dt.year == today.year and dt.month == today.month:
        return today
    if dt.month == 12:
        return dt.replace(day=31)
    return dt.replace(month=dt.month + 1, day=1) - timedelta(days=1)


def parse_enel_hourly_data(data: dict) -> dict[date, list[HourlyPoint]]:
    """Parse the Enel API response into a ``{date: [HourlyPoint]}`` mapping.

    Cumulative totals span day boundaries, so the returned values form a
    strictly-increasing sequence over the entire response period.

    Args:
        data: Raw JSON-decoded API response dict.

    Returns:
        Ordered mapping of calendar date → list of hourly points for that day.

    Raises:
        ValueError: If the expected ``hourlyConsumption`` aggregation is absent.
    """
    aggregations: list = (
        data.get("data", {}).get("aggregationResult", {}).get("aggregations", [])
    )
    hourly = next(
        (a for a in aggregations if a.get("referenceID") == "hourlyConsumption"),
        None,
    )
    if hourly is None:
        raise ValueError("No hourlyConsumption aggregation found in API response")

    results_by_date: dict[date, list[HourlyPoint]] = {}
    cumulative_offset = 0.0

    for day_result in sorted(
        hourly.get("results", []),
        key=lambda r: datetime.strptime(r["date"], DATE_FMT),
    ):
        day_date = datetime.strptime(day_result["date"], DATE_FMT).date()
        running_total = cumulative_offset
        points: list[HourlyPoint] = []

        for bin_entry in day_result.get("binValues", []):
            hour = int(bin_entry["name"][1:])  # "H1" → 1, "H24" → 24
            ts = datetime.combine(day_date, datetime.min.time()) + timedelta(hours=hour - 1)
            running_total += bin_entry["value"]
            points.append(
                HourlyPoint(timestamp=ts, kwh=bin_entry["value"], cumulative_kwh=running_total)
            )

        results_by_date[day_date] = points
        if points:
            cumulative_offset = points[-1].cumulative_kwh

    return results_by_date


def flatten_sorted(data_by_date: dict[date, list[HourlyPoint]]) -> list[HourlyPoint]:
    """Return all points from *data_by_date* in ascending timestamp order."""
    return sorted(
        (p for points in data_by_date.values() for p in points),
        key=lambda p: p.timestamp,
    )


def build_stat_rows(
    points: list[HourlyPoint],
    cumulative_offset: float,
    price_per_kwh: float,
) -> tuple[list[dict], list[dict], float]:
    """Build parallel kWh and EUR statistic-row lists from *points*.

    Args:
        points: Chronologically sorted hourly readings.
        cumulative_offset: Value to add to each point's relative cumulative.
        price_per_kwh: EUR/kWh multiplier for the cost series.

    Returns:
        ``(stats_kw, stats_cost, final_cumulative)`` where each stats list
        contains ``{"start": utc_datetime, "sum": float}`` dicts ready for
        ``async_add_external_statistics``.
    """
    from homeassistant.util.dt import as_utc  # imported here to keep module HA-free at test time

    stats_kw: list[dict] = []
    stats_cost: list[dict] = []
    final_cumulative = cumulative_offset

    for point in points:
        final_cumulative = point.cumulative_kwh + cumulative_offset
        utc_ts = as_utc(point.timestamp)
        stats_kw.append({"start": utc_ts, "sum": final_cumulative})
        stats_cost.append({"start": utc_ts, "sum": final_cumulative * price_per_kwh})

    return stats_kw, stats_cost, final_cumulative
