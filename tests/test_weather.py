"""Tests for the weather-aware buffer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from commutecompass.config import WeatherConfig
from commutecompass.timeutil import NYC_TZ
from commutecompass.weather import weather_buffer

AT = datetime(2026, 5, 8, 14, 30, tzinfo=NYC_TZ)  # → forecast hour "...T14:00"


def _fetcher(hourly: dict[str, Any]) -> Any:
    return lambda lat, lon, url: hourly


def _hourly(*, precip: float, prob: int, snow: float) -> dict[str, Any]:
    return {
        "time": [
            int(datetime(2026, 5, 8, hour, tzinfo=NYC_TZ).timestamp()) for hour in (13, 14)
        ],
        "precipitation": [0.0, precip],
        "precipitation_probability": [5, prob],
        "snowfall": [0.0, snow],
    }


def _raises(*a: Any, **k: Any) -> Any:
    raise AssertionError("fetcher should not be called")


def _boom(*a: Any, **k: Any) -> Any:
    raise RuntimeError("network down")


def test_fall_back_forecast_distinguishes_repeated_hour() -> None:
    first = datetime(2026, 11, 1, 5, tzinfo=UTC)
    second = datetime(2026, 11, 1, 6, tzinfo=UTC)
    hourly = {
        "time": [int(first.timestamp()), int(second.timestamp())],
        "precipitation": [0.0, 1.0], "precipitation_probability": [0, 100],
        "snowfall": [0.0, 0.0],
    }
    cfg = WeatherConfig(enabled=True, rain_buffer_minutes=12)
    assert weather_buffer(40.7, -74.0, first, cfg, fetcher=_fetcher(hourly)) == (0, None)
    assert weather_buffer(40.7, -74.0, second, cfg, fetcher=_fetcher(hourly)) == (12, "rain")


@pytest.mark.parametrize("field", ["precipitation", "precipitation_probability", "snowfall"])
@pytest.mark.parametrize("invalid", [10**1000, float("nan"), float("inf"), float("-inf")])
def test_malformed_forecast_numeric_values_fail_clear(field: str, invalid: object) -> None:
    hourly = _hourly(precip=2.0, prob=100, snow=1.0)
    hourly[field][1] = invalid
    cfg = WeatherConfig(enabled=True)
    assert weather_buffer(40.7, -74.0, AT, cfg, fetcher=_fetcher(hourly)) == (0, None)


def test_spring_forward_forecast_uses_utc_hour() -> None:
    before = datetime(2026, 3, 8, 6, tzinfo=UTC)
    after = datetime(2026, 3, 8, 7, tzinfo=UTC)
    hourly = {
        "time": [int(before.timestamp()), int(after.timestamp())],
        "precipitation": [0.0, 1.0], "precipitation_probability": [0, 100],
        "snowfall": [0.0, 0.0],
    }
    cfg = WeatherConfig(enabled=True, rain_buffer_minutes=12)
    assert weather_buffer(40.7, -74.0, before, cfg, fetcher=_fetcher(hourly)) == (0, None)
    assert weather_buffer(40.7, -74.0, after, cfg, fetcher=_fetcher(hourly)) == (12, "rain")


def test_disabled_returns_zero() -> None:
    cfg = WeatherConfig(enabled=False)
    # Fetcher would raise if called — disabled must short-circuit.
    assert weather_buffer(40.7, -74.0, AT, cfg, fetcher=_raises) == (0, None)


def test_clear_forecast_returns_zero() -> None:
    cfg = WeatherConfig(enabled=True)
    hourly = _hourly(precip=0.0, prob=5, snow=0.0)
    assert weather_buffer(40.7, -74.0, AT, cfg, fetcher=_fetcher(hourly)) == (0, None)


def test_rain_by_probability() -> None:
    cfg = WeatherConfig(enabled=True, rain_buffer_minutes=12, precip_probability_threshold=50)
    hourly = _hourly(precip=0.0, prob=80, snow=0.0)
    buf = weather_buffer(40.7, -74.0, AT, cfg, fetcher=_fetcher(hourly))
    assert buf.minutes == 12
    assert buf.reason == "rain"


def test_rain_below_threshold_is_clear() -> None:
    cfg = WeatherConfig(enabled=True, precip_probability_threshold=50)
    hourly = _hourly(precip=0.0, prob=30, snow=0.0)
    assert weather_buffer(40.7, -74.0, AT, cfg, fetcher=_fetcher(hourly)).minutes == 0


def test_snow_takes_priority_and_uses_snow_buffer() -> None:
    cfg = WeatherConfig(enabled=True, rain_buffer_minutes=10, snow_buffer_minutes=25)
    hourly = _hourly(precip=2.0, prob=90, snow=1.5)
    buf = weather_buffer(40.7, -74.0, AT, cfg, fetcher=_fetcher(hourly))
    assert buf.minutes == 25
    assert buf.reason == "snow"


def test_fetch_error_swallowed() -> None:
    cfg = WeatherConfig(enabled=True)
    assert weather_buffer(40.7, -74.0, AT, cfg, fetcher=_boom) == (0, None)


def test_missing_hour_returns_zero() -> None:
    cfg = WeatherConfig(enabled=True)
    hourly = {"time": ["2026-05-08T09:00"], "precipitation": [9.0], "snowfall": [0.0]}
    assert weather_buffer(40.7, -74.0, AT, cfg, fetcher=_fetcher(hourly)).minutes == 0
