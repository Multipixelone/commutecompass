"""Tests for the weather-aware buffer."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from commutecompass.config import WeatherConfig
from commutecompass.timeutil import NYC_TZ
from commutecompass.weather import weather_buffer


AT = datetime(2026, 5, 8, 14, 30, tzinfo=NYC_TZ)  # → forecast hour "...T14:00"


def _fetcher(hourly: dict[str, Any]) -> Any:
    return lambda lat, lon, url: hourly


def _hourly(*, precip: float, prob: int, snow: float) -> dict[str, Any]:
    return {
        "time": ["2026-05-08T13:00", "2026-05-08T14:00"],
        "precipitation": [0.0, precip],
        "precipitation_probability": [5, prob],
        "snowfall": [0.0, snow],
    }


def _raises(*a: Any, **k: Any) -> Any:
    raise AssertionError("fetcher should not be called")


def _boom(*a: Any, **k: Any) -> Any:
    raise RuntimeError("network down")


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
