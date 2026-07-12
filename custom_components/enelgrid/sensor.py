"""EnelGrid sensor platform.

Registers the consumption and monthly sensors, drives daily updates, and
manages the one-time historical back-fill task.  All API parsing lives in
:mod:`.parser`; this module focuses exclusively on Home Assistant integration.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.persistent_notification import async_create
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    clear_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util.dt import as_utc  # noqa: F401 — re-exported for parser at runtime

from .const import (
    CONF_PASSWORD,
    CONF_POD,
    CONF_PRICE_PER_KWH,
    CONF_USER_NUMBER,
    CONF_USERNAME,
    DOMAIN,
)
from .login import EnelGridSession
from .parser import (
    DATE_FMT,
    HourlyPoint,
    MonthBatch,
    build_stat_rows,
    flatten_sorted,
    month_last_day,
    parse_enel_hourly_data,
)
from datetime import date

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(days=1)

#: How far back to look for an adjacent statistic when resolving the offset.
_OFFSET_LOOKBACK = timedelta(hours=2)

#: Key under hass.data used to share the monthly sensor between setup and updates.
_MONTHLY_SENSOR_KEY = "enelgrid_monthly_sensor"

#: Entry-data flag keys (kept here so typos are caught at import time).
_FLAG_CLEAR_STATS = "clear_statistics_needed"
_FLAG_FETCH_NEEDED = "historical_fetch_needed"
_FLAG_FETCH_DONE = "historical_fetch_completed"


def _normalize_pod(pod: str) -> str:
    """Return a pod string safe for use in entity/statistic IDs."""
    return pod.lower().replace("-", "_").replace(".", "_")


def _statistic_ids(pod: str) -> tuple[str, str]:
    """Return ``(consumption_id, cost_id)`` statistic IDs for *pod*.
    
    Consumption ID matches the sensor entity_id so the energy dashboard
    finds both the entity and its hourly statistics together.
    """
    base = f"sensor:enelgrid_{_normalize_pod(pod)}"
    return f"{base}_consumption", f"{base}_kw_cost"


def _statistic_metadata(pod: str, statistic_id: str, name: str, unit: str, unit_class: str | None = None) -> dict:
    """Return a recorder statistics metadata dict."""
    return {
        "has_mean": False,
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": name,
        "source": "sensor",
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
        "unit_class": unit_class,
    }


def _update_entry(hass, entry, **kwargs) -> None:
    """Persist *kwargs* into *entry*.data."""
    hass.config_entries.async_update_entry(entry, data={**entry.data, **kwargs})


async def _recorder_job(hass, func, *args):
    """Run *func(*args)* in the recorder executor and return the result."""
    return await get_instance(hass).async_add_executor_job(func, *args)


async def _clear_statistics_if_needed(hass, entry, pod: str) -> None:
    """Clear recorder statistics once when the migration flag is set."""
    if not entry.data.get(_FLAG_CLEAR_STATS, False):
        return

    _LOGGER.info("[EnelGrid] Clearing old statistics (migration)...")
    stat_consumption, stat_cost = _statistic_ids(pod)
    recorder = get_instance(hass)
    try:
        await recorder.async_add_executor_job(
            clear_statistics, recorder, [stat_consumption, stat_cost]
        )
        _LOGGER.info("[EnelGrid] Cleared: %s, %s", stat_consumption, stat_cost)
    except Exception:
        _LOGGER.exception("[EnelGrid] Failed to clear statistics — continuing anyway")

    _update_entry(hass, entry, **{_FLAG_CLEAR_STATS: False})


async def historical_fetch_task(hass, entry, sensor: EnelGridConsumptionSensor) -> None:
    """Fetch all available historical data month-by-month and save oldest-first."""
    _LOGGER.info("[EnelGrid] Starting full historical fetch...")

    pod: str = entry.data[CONF_POD]
    price_per_kwh: float = entry.data[CONF_PRICE_PER_KWH]

    await _clear_statistics_if_needed(hass, entry, pod)

    months: list[MonthBatch] = await _collect_months(entry, pod)
    months.reverse()  # oldest first
    _LOGGER.info("[EnelGrid] Collected %d months — saving chronologically...", len(months))

    stat_consumption, _ = _statistic_ids(pod)
    cumulative_offset = await sensor.get_last_cumulative_kwh(stat_consumption)
    _LOGGER.info("[EnelGrid] Starting cumulative offset: %.2f kWh", cumulative_offset)

    for batch in months:
        try:
            cumulative_offset = await sensor.save_to_home_assistant(
                batch["data_points"], pod, price_per_kwh,
                cumulative_offset=cumulative_offset,
            )
            _LOGGER.info("[EnelGrid] Saved %s — offset now %.2f kWh", batch["month"], cumulative_offset)
        except Exception:
            _LOGGER.exception("[EnelGrid] Error saving %s", batch["month"])

    _update_entry(hass, entry, **{_FLAG_FETCH_NEEDED: False, _FLAG_FETCH_DONE: True})
    _LOGGER.info("[EnelGrid] Historical fetch complete: %d months saved", len(months))


async def _collect_months(entry, pod: str) -> list[MonthBatch]:
    """Open one session and page backwards month-by-month until the API returns nothing."""
    session = EnelGridSession(
        entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD],
        pod, entry.data[CONF_USER_NUMBER],
    )
    current_date = datetime.now()
    batches: list[MonthBatch] = []

    try:
        while True:
            first_day = current_date.replace(day=1)
            validity_from = first_day.strftime(DATE_FMT)
            validity_to = month_last_day(current_date).strftime(DATE_FMT)
            month_label = current_date.strftime("%Y-%m")

            _LOGGER.info("[EnelGrid] Fetching %s (%s → %s)...", month_label, validity_from, validity_to)

            try:
                raw = await session.fetch_consumption_data(validity_from, validity_to)
                data_points = parse_enel_hourly_data(raw)
            except Exception:
                _LOGGER.exception("[EnelGrid] Error fetching %s — stopping", month_label)
                break

            if not data_points:
                _LOGGER.info("[EnelGrid] No data for %s — reached historical limit", month_label)
                break

            batches.append(MonthBatch(month=month_label, data_points=data_points))
            _LOGGER.info("[EnelGrid] %s: %d days", month_label, len(data_points))
            current_date = first_day - timedelta(days=1)
    finally:
        await session.close()

    return batches


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up enelgrid sensors from a config entry."""
    pod: str = entry.data[CONF_POD]

    consumption_sensor = EnelGridConsumptionSensor(hass, entry)
    monthly_sensor = EnelGridMonthlySensor(pod)

    hass.data.setdefault(_MONTHLY_SENSOR_KEY, {})[entry.entry_id] = monthly_sensor
    async_add_entities([consumption_sensor, monthly_sensor])
    _LOGGER.info(
        "[EnelGrid] Sensors registered: %s, %s",
        consumption_sensor.entity_id, monthly_sensor.entity_id,
    )

    if entry.data.get(_FLAG_FETCH_NEEDED, False):
        _LOGGER.info("[EnelGrid] Scheduling historical fetch...")
        hass.async_create_task(historical_fetch_task(hass, entry, consumption_sensor))
    else:
        hass.async_create_task(consumption_sensor.async_update())

    async def _daily_update(_now) -> None:
        await consumption_sensor.async_update()

    async_track_time_interval(hass, _daily_update, SCAN_INTERVAL)


class EnelGridConsumptionSensor(SensorEntity):
    """Fetches and imports hourly consumption data from the Enel Grid API."""

    def __init__(self, hass, entry) -> None:
        self.hass = hass
        self.entry_id: str = entry.entry_id
        self._pod: str = entry.data[CONF_POD]
        self._price_per_kwh: float = entry.data[CONF_PRICE_PER_KWH]
        self._session_kwargs = {
            "username": entry.data[CONF_USERNAME],
            "password": entry.data[CONF_PASSWORD],
            "pod": self._pod,
            "user_number": entry.data[CONF_USER_NUMBER],
        }
        self._attr_name = "enelgrid Daily Import"
        self._attr_unique_id = f"{self._pod}_daily_import"
        self.entity_id = f"sensor.enelgrid_{_normalize_pod(self._pod)}_daily_import"
        self._state: str | None = None

    @property
    def state(self) -> str | None:
        return self._state

    def _handle_auth_error(self, err: ConfigEntryAuthFailed) -> None:
        """Notify the user and trigger the re-auth config flow."""
        self._state = "Login error"
        async_create(
            self.hass,
            message=f"Login failed. Please check your credentials. {err}",
            title="EnelGrid Login Error",
        )
        self.hass.async_create_task(
            self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "reauth", "entry_id": self.entry_id}, data={}
            )
        )

    async def async_update(self) -> None:
        """Fetch today's data and persist it to the recorder."""
        session = EnelGridSession(**self._session_kwargs)
        try:
            raw = await session.fetch_consumption_data()
            data_by_date = parse_enel_hourly_data(raw)
            if not data_by_date:
                _LOGGER.warning("[EnelGrid] No hourly data returned")
                self._state = "No data"
                return
            await self.save_to_home_assistant(data_by_date, self._pod, self._price_per_kwh)
            await self._update_monthly_sensor(data_by_date)
            self._state = "Imported"
        except ConfigEntryAuthFailed as err:
            self._handle_auth_error(err)
        except Exception:
            _LOGGER.exception("[EnelGrid] Failed to update sensor data")
            self._state = "Error"
        finally:
            await session.close()

    async def save_to_home_assistant(
        self,
        data_by_date: dict[date, list[HourlyPoint]],
        pod: str,
        price_per_kwh: float,
        cumulative_offset: float | None = None,
    ) -> float:
        """Write hourly statistics to the HA recorder.

        Flattens *data_by_date*, resolves the running-total offset if not
        supplied, builds paired kWh / EUR stat rows, and calls
        ``async_add_external_statistics`` for both series.

        Args:
            data_by_date: Parsed hourly readings grouped by date.
            pod: POD identifier (used to derive statistic IDs).
            price_per_kwh: EUR per kWh for cost calculation.
            cumulative_offset: Seed for the running total.  When ``None`` the
                               value is looked up from existing recorder data.

        Returns:
            The final cumulative kWh after all points are saved.
        """
        stat_consumption, stat_cost = _statistic_ids(pod)
        metadata_kw = _statistic_metadata(pod, stat_consumption, f"Enel {pod} Consumption", "kWh", unit_class="energy")
        metadata_cost = _statistic_metadata(pod, stat_cost, f"Enel {pod} Cost", "EUR")

        flat_points = flatten_sorted(data_by_date)
        if not flat_points:
            _LOGGER.warning("[EnelGrid] No data points to save")
            return cumulative_offset or 0.0

        if cumulative_offset is None:
            cumulative_offset = await self._resolve_offset(stat_consumption, flat_points[0].timestamp)

        stats_kw, stats_cost, final_cumulative = build_stat_rows(
            flat_points, cumulative_offset, price_per_kwh
        )

        try:
            async_add_external_statistics(self.hass, metadata_kw, stats_kw)
            async_add_external_statistics(self.hass, metadata_cost, stats_cost)
            _LOGGER.debug(
                "[EnelGrid] Saved %d points — final sum: %.2f kWh",
                len(stats_kw), final_cumulative,
            )
        except HomeAssistantError:
            _LOGGER.exception("[EnelGrid] Failed to persist statistics for %s", stat_consumption)

        return final_cumulative

    async def _resolve_offset(self, statistic_id: str, first_point_time: datetime) -> float:
        """Return the cumulative sum recorded just before *first_point_time*.

        Checks the ``_OFFSET_LOOKBACK`` window first; falls back to the most
        recent statistic overall; returns ``0.0`` when no history exists.
        """
        # 1. Look for a statistic in the window immediately before our data.
        adjacent = await _recorder_job(
            self.hass,
            statistics_during_period,
            self.hass,
            first_point_time - _OFFSET_LOOKBACK,
            first_point_time,
            [statistic_id],
            "hour",
            None,
            {"sum"},
        )
        if adjacent.get(statistic_id):
            offset = adjacent[statistic_id][-1]["sum"]
            _LOGGER.info("[EnelGrid] Offset from adjacent window: %.2f kWh", offset)
            return offset

        # 2. Fall back to the last-ever recorded statistic.
        last = await _recorder_job(
            self.hass, get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        if not (last and statistic_id in last):
            _LOGGER.info("[EnelGrid] No prior statistics — starting from 0")
            return 0.0

        last_stat = last[statistic_id][0]
        last_time = datetime.fromtimestamp(last_stat["start"], first_point_time.tzinfo)
        if last_time >= first_point_time:
            _LOGGER.warning(
                "[EnelGrid] Existing statistics are newer than current batch — using offset 0"
            )
            return 0.0

        offset = last_stat["sum"]
        _LOGGER.info("[EnelGrid] Offset from last statistic: %.2f kWh", offset)
        return offset

    async def get_last_cumulative_kwh(self, statistic_id: str) -> float:
        """Return the most-recent cumulative kWh for *statistic_id*, or ``0.0``."""
        last = await _recorder_job(
            self.hass, get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        if not (last and statistic_id in last):
            return 0.0
        value: float = last[statistic_id][0]["sum"]
        _LOGGER.info("[EnelGrid] Last recorded sum for %s: %.2f kWh", statistic_id, value)
        return value

    async def _update_monthly_sensor(self, data_by_date: dict[date, list[HourlyPoint]]) -> None:
        monthly: EnelGridMonthlySensor | None = (
            self.hass.data.get(_MONTHLY_SENSOR_KEY, {}).get(self.entry_id)
        )
        if monthly and (last_points := data_by_date.get(max(data_by_date))):
            total_kwh = last_points[-1].cumulative_kwh
            monthly.set_total(total_kwh)
            _LOGGER.info("[EnelGrid] Monthly sensor → %.2f kWh", total_kwh)

    # Public alias — keeps any external callers unbroken.
    async def update_monthly_sensor(
        self, data_by_date: dict[date, list[HourlyPoint]], entry_id: str
    ) -> None:
        await self._update_monthly_sensor(data_by_date)


class EnelGridMonthlySensor(SensorEntity):
    """Shows cumulative consumption for the current period.
    Hourly statistics are imported under the same entity_id so the
    Energy Dashboard can use both the hourly data and the state value.
    """

    def __init__(self, pod: str) -> None:
        object_id = f"enelgrid_{_normalize_pod(pod)}_consumption"
        self.entity_id = f"sensor.{object_id}"
        self._attr_name = f"Enel {pod} Consumption"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = "total_increasing"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_unique_id = f"{_normalize_pod(pod)}_consumption"
        self._attr_extra_state_attributes = {"source": "enelgrid"}
        self._state: float = 0.0

    @property
    def state(self) -> float:
        return self._state

    def set_total(self, new_total: float) -> None:
        """Update the displayed total and push the state change to HA."""
        self._state = new_total
        self.async_write_ha_state()
