"""Weather-aware departure buffer via the Open-Meteo forecast API.

A clear-sky commute and a snowy one need different head starts.  When rain or
snow is likely around the time the user would leave, we pad the buffer so the
alarm fires earlier.  Open-Meteo is free and keyless, so this needs no secret.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, NamedTuple, Optional

import httpx

from commutecompass.config import WeatherConfig
from commutecompass.retry import retry
from commutecompass.timeutil import to_nyc

logger = logging.getLogger(__name__)


class WeatherBuffer(NamedTuple):
    minutes: int
    reason: Optional[str]  # human-readable note, e.g. "rain", or None when clear


_CLEAR = WeatherBuffer(0, None)


def weather_buffer(
    lat: float,
    lon: float,
    at_time: datetime,
    config: WeatherConfig,
    *,
    fetcher: Optional[Any] = None,
) -> WeatherBuffer:
    """Return extra buffer minutes (and a reason) for precipitation at ``at_time``.

    Returns ``WeatherBuffer(0, None)`` when disabled, on any fetch/parse error,
    or when the forecast is clear — weather is an enhancement, never a reason to
    fail a plan.  ``fetcher`` is injectable for tests.
    """
    if not config.enabled:
        return _CLEAR

    fetch = fetcher or _fetch_forecast
    try:
        hourly = fetch(lat, lon, config.forecast_url)
    except Exception as exc:
        logger.debug("weather fetch failed (lat=%s lon=%s): %s", lat, lon, exc)
        return _CLEAR

    return _buffer_from_hourly(hourly, at_time, config)


def _fetch_forecast(lat: float, lon: float, forecast_url: str) -> dict[str, Any]:
    """Fetch hourly precipitation/snowfall from Open-Meteo for the next 2 days."""
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "hourly": "precipitation,precipitation_probability,snowfall",
        "timezone": "America/New_York",
        "forecast_days": "2",
    }

    def _do() -> dict[str, Any]:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(forecast_url, params=params)
            resp.raise_for_status()
            data = resp.json()
        hourly = data.get("hourly") if isinstance(data, dict) else None
        return hourly if isinstance(hourly, dict) else {}

    return retry(_do, attempts=2, label="open-meteo")


def _buffer_from_hourly(
    hourly: dict[str, Any], at_time: datetime, config: WeatherConfig
) -> WeatherBuffer:
    """Pick the forecast hour matching ``at_time`` and derive a buffer."""
    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        return _CLEAR

    # Open-Meteo hour stamps are local ISO strings like "2026-06-23T08:00".
    target = to_nyc(at_time).strftime("%Y-%m-%dT%H:00")
    try:
        idx = times.index(target)
    except ValueError:
        return _CLEAR

    snowfall = _at(hourly.get("snowfall"), idx)
    precip = _at(hourly.get("precipitation"), idx)
    prob = _at(hourly.get("precipitation_probability"), idx)

    # Snow dominates — it slows every mode the most.
    if snowfall is not None and snowfall > 0:
        return WeatherBuffer(config.snow_buffer_minutes, "snow")

    likely = (prob is not None and prob >= config.precip_probability_threshold) or (
        precip is not None and precip > 0
    )
    if likely:
        return WeatherBuffer(config.rain_buffer_minutes, "rain")

    return _CLEAR


def _at(series: Any, idx: int) -> Optional[float]:
    """Safely read index ``idx`` from a forecast series, as a float."""
    if not isinstance(series, list) or idx >= len(series):
        return None
    value = series[idx]
    if isinstance(value, (int, float)):
        return float(value)
    return None
